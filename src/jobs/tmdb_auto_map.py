import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import select
import httpx

from .. import crud, models
from .base import BaseJob
from ..rate_limiter import RateLimiter
from ..task_manager import TaskManager, TaskSuccess
from ..scraper_manager import ScraperManager
from ..metadata_manager import MetadataSourceManager
from ..ai_matcher import AIMatcher

class TmdbAutoMapJob(BaseJob):
    job_type = "tmdbAutoScrape"
    job_name = "TMDB自动刮削与剧集组映射"
    description = "自动从TMDB刮削已导入作品的别名、剧集组信息，更新分集映射关系。帮助解决分集顺序不一致的问题。"

    # 修正：此任务不涉及弹幕下载，因此移除不必要的 rate_limiter 依赖
    # 修正：接收正确的依赖项
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], task_manager: TaskManager, metadata_manager: MetadataSourceManager):
        # 由于此任务的依赖项与基类不同，我们不调用 super().__init__，
        # 而是直接初始化此任务所需的属性。
        self._session_factory = session_factory
        self.task_manager = task_manager
        self.metadata_manager = metadata_manager
        self.logger = logging.getLogger(self.__class__.__name__)


    async def run(self, session: AsyncSession, progress_callback: Callable):
        """
        定时任务的核心逻辑。
        1. 获取所有TV系列作品
        2. 对于没有TMDB ID的作品，通过标题搜索TMDB获取ID
        3. 刮削别名信息
        4. 获取并映射剧集组信息
        """
        self.logger.info(f"开始执行 [{self.job_name}] 定时任务...")
        await progress_callback(0, "正在初始化...")

        # 为元数据管理器调用创建一个虚拟用户对象
        user = models.User(id=0, username="scheduled_task")

        # 初始化AI matcher (如果启用)
        ai_matcher = None
        ai_recognition_enabled = False
        ai_alias_correction_enabled = False
        try:
            ai_match_enabled = await crud.get_config_value(session, "aiMatchEnabled", "false") == "true"
            ai_recognition_enabled = await crud.get_config_value(session, "aiRecognitionEnabled", "false") == "true"
            ai_alias_correction_enabled = await crud.get_config_value(session, "aiAliasCorrectionEnabled", "false") == "true"

            if ai_match_enabled and ai_recognition_enabled:
                self.logger.info("AI辅助识别已启用")
                if ai_alias_correction_enabled:
                    self.logger.info("AI别名修正已启用")

                # 动态注册AI提示词配置(如果不存在则创建,使用硬编码默认值)
                from ..ai_matcher import DEFAULT_AI_MATCH_PROMPT, DEFAULT_AI_RECOGNITION_PROMPT, DEFAULT_AI_ALIAS_VALIDATION_PROMPT
                await crud.initialize_configs(session, {
                    "aiMatchPrompt": (DEFAULT_AI_MATCH_PROMPT, "AI智能匹配提示词"),
                    "aiRecognitionPrompt": (DEFAULT_AI_RECOGNITION_PROMPT, "AI辅助识别提示词"),
                    "aiAliasValidationPrompt": (DEFAULT_AI_ALIAS_VALIDATION_PROMPT, "AI别名验证提示词")
                })

                # 读取提示词配置
                # 注意: 此时数据库中一定存在这些键(上面已经初始化),直接读取即可
                # 即使值为空字符串也会被读取,不会使用硬编码兜底
                ai_match_prompt = await crud.get_config_value(session, "aiMatchPrompt", "")
                ai_recognition_prompt = await crud.get_config_value(session, "aiRecognitionPrompt", "")
                ai_alias_validation_prompt = await crud.get_config_value(session, "aiAliasValidationPrompt", "")

                config = {
                    "ai_match_provider": await crud.get_config_value(session, "aiMatchProvider", "deepseek"),
                    "ai_match_api_key": await crud.get_config_value(session, "aiMatchApiKey", ""),
                    "ai_match_base_url": await crud.get_config_value(session, "aiMatchBaseUrl", ""),
                    "ai_match_model": await crud.get_config_value(session, "aiMatchModel", "deepseek-chat"),
                    "ai_match_prompt": ai_match_prompt,
                    "ai_recognition_prompt": ai_recognition_prompt,
                    "ai_alias_validation_prompt": ai_alias_validation_prompt
                }
                ai_matcher = AIMatcher(config)
        except Exception as e:
            self.logger.warning(f"初始化AI matcher失败: {e}, 将使用传统搜索")

        # 获取所有作品(TV系列和电影/剧场版)
        from ..orm_models import Anime, AnimeMetadata
        stmt = (
            select(
                Anime.id.label("animeId"),
                Anime.title,
                Anime.year,
                Anime.type,
                AnimeMetadata.tmdbId,
                AnimeMetadata.tmdbEpisodeGroupId
            )
            .outerjoin(AnimeMetadata, Anime.id == AnimeMetadata.animeId)
            .where(Anime.type.in_(['tv_series', 'movie']))
        )
        result = await session.execute(stmt)
        shows_to_update = [dict(row) for row in result.mappings()]

        total_shows = len(shows_to_update)
        self.logger.info(f"找到 {total_shows} 个作品需要处理(TV系列和电影)。")
        await progress_callback(5, f"找到 {total_shows} 个作品待处理")

        processed_count = 0
        scraped_count = 0
        mapped_count = 0

        for i, show in enumerate(shows_to_update):
            current_progress = 5 + int((i / total_shows) * 95) if total_shows > 0 else 95
            anime_id = show['animeId']
            title = show['title']
            year = show.get('year')
            tmdb_id = show.get('tmdbId')

            await progress_callback(current_progress, f"正在处理: {title} ({i+1}/{total_shows})")
            self.logger.info(f"正在处理: '{title}' (Anime ID: {anime_id}, TMDB ID: {tmdb_id or '无'})")

            try:
                # 初始化变量
                use_episode_group = False
                recognized_season = None
                search_type = show.get('type', 'tv_series')  # 提前初始化,供AI验证别名使用

                # 步骤 1: 如果没有TMDB ID，尝试通过标题搜索获取
                if not tmdb_id:
                    self.logger.info(f"'{title}' 没有TMDB ID，尝试搜索...")
                    try:
                        # 使用AI标准化标题 (如果启用)
                        search_title = title
                        search_year = year

                        if ai_matcher and ai_recognition_enabled:
                            try:
                                recognition_result = await ai_matcher.recognize_title(
                                    title=title,
                                    year=year,
                                    anime_type=search_type
                                )
                                if recognition_result:
                                    search_title = recognition_result.get("search_title", title)
                                    search_year = recognition_result.get("year", year)
                                    search_type = recognition_result.get("type", search_type)
                                    use_episode_group = recognition_result.get("use_episode_group", False)
                                    recognized_season = recognition_result.get("season")

                                    if recognized_season:
                                        self.logger.info(f"AI识别到季度: {recognized_season}")
                                    if use_episode_group:
                                        self.logger.info(f"AI识别: 该作品需要使用剧集组")

                                    self.logger.info(f"AI标准化: '{title}' → '{search_title}' (year={search_year}, type={search_type})")
                            except Exception as e:
                                self.logger.warning(f"AI标准化失败: {e}, 使用原标题搜索")

                        # 根据类型选择mediaType
                        media_type = "movie" if search_type == "movie" else "tv"

                        search_results = await self.metadata_manager.search("tmdb", search_title, user, mediaType=media_type)
                        if search_results:
                            # 智能选择最佳匹配结果
                            # 优先级: 类型匹配 > 年份匹配 > 第一个结果
                            best_match = search_results[0]

                            # 如果AI识别到季度信息,说明肯定是TV类型
                            if recognized_season is not None and media_type == "tv":
                                self.logger.info(f"检测到季度信息(season={recognized_season}),强制使用TV类型筛选")
                                # 筛选TV类型的结果
                                tv_results = [r for r in search_results if r.type == "tv" or not r.type]
                                if tv_results:
                                    search_results = tv_results
                                    best_match = tv_results[0]
                                    self.logger.info(f"筛选后剩余 {len(tv_results)} 个TV类型结果")

                            # 年份匹配
                            if search_year:
                                # 尝试找到年份匹配的结果
                                for result in search_results:
                                    if result.year == search_year:
                                        best_match = result
                                        self.logger.info(f"找到年份匹配的结果: {result.title} ({result.year})")
                                        break

                            tmdb_id = best_match.tmdbId
                            self.logger.info(f"为 '{title}' 找到TMDB ID: {tmdb_id} (类型: {best_match.type or 'unknown'})")

                            # 保存TMDB ID到数据库
                            await crud.update_metadata_if_empty(
                                session,
                                anime_id,
                                tmdb_id=tmdb_id
                            )
                            await session.commit()
                            scraped_count += 1
                        else:
                            self.logger.warning(f"未能为 '{title}' 找到TMDB搜索结果。")
                            continue
                    except Exception as e:
                        self.logger.error(f"搜索 '{title}' 时发生错误: {e}")
                        continue

                # 步骤 2: 获取媒体详情，包括别名
                # 根据作品类型决定mediaType参数
                media_type_for_details = "movie" if search_type == "movie" else "tv"
                details = await self.metadata_manager.get_details("tmdb", tmdb_id, user, mediaType=media_type_for_details)
                if not details:
                    self.logger.warning(f"未能从 TMDB 获取 '{title}' (ID: {tmdb_id}) 的详情 (mediaType={media_type_for_details})。")
                    continue

                # 步骤 3: 更新别名
                # 注意: 电影类型不使用AI验证别名,因为电影标题通常包含系列名+副标题
                # 例如"名侦探柯南 绀碧之棺",AI可能无法正确识别属于该电影的别名
                # TV系列可以使用AI验证,因为标题通常是系列名

                if ai_matcher and ai_recognition_enabled and search_type == "tv_series":
                    # 仅对TV系列使用AI验证别名
                    # 收集所有别名
                    all_aliases = []
                    if details.nameEn: all_aliases.append(details.nameEn)
                    if details.nameJp: all_aliases.append(details.nameJp)
                    if details.nameRomaji: all_aliases.append(details.nameRomaji)
                    if details.aliasesCn: all_aliases.extend(details.aliasesCn)

                    if all_aliases:
                        self.logger.info(f"正在使用AI验证 '{title}' 的 {len(all_aliases)} 个别名...")
                        validated_aliases = ai_matcher.validate_aliases(
                            title=title,
                            year=year,
                            anime_type=search_type,
                            aliases=all_aliases
                        )

                        if validated_aliases:
                            # 使用AI验证后的别名
                            aliases_to_update = {
                                "name_en": validated_aliases.get("nameEn"),
                                "name_jp": validated_aliases.get("nameJp"),
                                "name_romaji": validated_aliases.get("nameRomaji"),
                                "aliases_cn": validated_aliases.get("aliasesCn", [])
                            }
                            if any(aliases_to_update.values()):
                                # 如果启用了AI别名修正,则强制更新
                                force_update = ai_alias_correction_enabled
                                await crud.update_anime_aliases_if_empty(session, anime_id, aliases_to_update, force_update=force_update)
                                if force_update:
                                    self.logger.info(f"为 '{title}' 强制更新了AI验证后的别名(AI修正模式)。")
                                else:
                                    self.logger.info(f"为 '{title}' 更新了AI验证后的别名。")
                        else:
                            self.logger.warning(f"AI别名验证失败,使用原始别名")
                            # 降级到原始别名
                            aliases_to_update = {
                                "name_en": details.nameEn,
                                "name_jp": details.nameJp,
                                "name_romaji": details.nameRomaji,
                                "aliases_cn": details.aliasesCn
                            }
                            if any(aliases_to_update.values()):
                                await crud.update_anime_aliases_if_empty(session, anime_id, aliases_to_update)
                                self.logger.info(f"为 '{title}' 更新了原始别名。")
                else:
                    # 电影类型或未启用AI,直接更新原始别名
                    aliases_to_update = {
                        "name_en": details.nameEn,
                        "name_jp": details.nameJp,
                        "name_romaji": details.nameRomaji,
                        "aliases_cn": details.aliasesCn
                    }
                    if any(aliases_to_update.values()):
                        await crud.update_anime_aliases_if_empty(session, anime_id, aliases_to_update)
                        if search_type == "movie":
                            self.logger.info(f"为电影 '{title}' 更新了TMDB原始别名(跳过AI验证)。")
                        else:
                            self.logger.info(f"为 '{title}' 更新了别名。")

                # 步骤 4: 智能季度匹配 - 两级查找逻辑 (仅适用于TV系列)
                # 第一级: 使用seasons信息进行匹配(方案A)
                # 第二级: 使用"Seasons"剧集组进行匹配(方案C)

                # 电影类型跳过剧集组处理
                if show.get('type') == 'movie':
                    self.logger.info(f"'{title}' 是电影类型,跳过剧集组处理。")
                    await session.commit()
                    processed_count += 1
                    continue

                tmdb_source = self.metadata_manager.sources.get("tmdb")
                if not tmdb_source or not hasattr(tmdb_source, 'get_all_episode_groups'):
                    self.logger.warning(f"TMDB源不支持 get_all_episode_groups 方法，跳过 '{title}' 的剧集组处理。")
                    continue

                season_matched = False
                groups_to_process = []
                all_groups = []

                # 第一级: 如果AI识别到季度信息,尝试使用seasons数据进行匹配
                if recognized_season is not None and details.seasons:
                    self.logger.info(f"第一级匹配: 使用seasons信息查找季度 {recognized_season}")

                    # 查找对应季度的season信息
                    target_season = None
                    for season in details.seasons:
                        if season.season_number == recognized_season:
                            target_season = season
                            break

                    if target_season:
                        self.logger.info(f"✓ 找到匹配的季度: {target_season.name} (ID: {target_season.id}, 集数: {target_season.episode_count})")
                        season_matched = True

                        # 获取所有剧集组,尝试找到匹配该季度的剧集组
                        all_groups = await tmdb_source.get_all_episode_groups(int(tmdb_id), user)
                        if all_groups:
                            self.logger.info(f"为 '{title}' 找到 {len(all_groups)} 个剧集组: {[g.get('name') for g in all_groups]}")

                            # 尝试找到匹配该季度的剧集组
                            for g in all_groups:
                                group_name = g.get('name', '').lower()

                                # 特殊处理第0季(特别季)
                                if recognized_season == 0:
                                    if ("special" in group_name or
                                        "season 0" in group_name or
                                        "s00" in group_name or
                                        "s0" in group_name or
                                        "特别" in group_name):
                                        groups_to_process.append(g)
                                        self.logger.info(f"✓ 找到特别季剧集组: {g.get('name')}")
                                else:
                                    # 匹配 "season 2", "第2季", "s02" 等格式
                                    if (f"season {recognized_season}" in group_name or
                                        f"第{recognized_season}季" in group_name or
                                        f"s{recognized_season:02d}" in group_name or
                                        f"s{recognized_season}" in group_name):
                                        groups_to_process.append(g)
                                        self.logger.info(f"✓ 找到匹配季度{recognized_season}的剧集组: {g.get('name')}")
                    else:
                        self.logger.warning(f"✗ 未找到季度 {recognized_season} 的seasons信息")

                # 第二级: 如果第一级没有找到,使用"Seasons"剧集组进行匹配
                if not season_matched or not groups_to_process:
                    self.logger.info(f"第二级匹配: 使用剧集组查找")

                    if not all_groups:
                        all_groups = await tmdb_source.get_all_episode_groups(int(tmdb_id), user)

                    if not all_groups:
                        self.logger.info(f"'{title}' (TMDB ID: {tmdb_id}) 没有找到任何剧集组。")
                        continue

                    self.logger.info(f"为 '{title}' 找到 {len(all_groups)} 个剧集组: {[g.get('name') for g in all_groups]}")

                    # 优先选择"Seasons"剧集组
                    seasons_group = None
                    for g in all_groups:
                        group_name = g.get('name', '').lower()
                        if 'seasons' in group_name:
                            seasons_group = g
                            self.logger.info(f"✓ 找到'Seasons'剧集组: {g.get('name')}")
                            break

                    if seasons_group:
                        groups_to_process = [seasons_group]
                    else:
                        # 如果没有"Seasons"剧集组,使用默认逻辑(type=1的剧集组)
                        groups_to_process = [g for g in all_groups if g.get('type') == 1]
                        if groups_to_process:
                            self.logger.info(f"未找到'Seasons'剧集组,使用默认逻辑(type=1)")

                if not groups_to_process:
                    self.logger.info(f"'{title}' 没有找到“原始播出顺序”(type=1)的剧集组，跳过映射更新。")
                    continue
                
                self.logger.info(f"为 '{title}' 选择了 {len(groups_to_process)} 个剧集组进行映射更新。")

                # 步骤 5: 为每个选定的剧集组，更新映射表
                for group in groups_to_process:
                    group_id = group.get('id')
                    if not group_id:
                        continue

                    self.logger.info(f"正在为 '{title}' 更新剧集组 '{group.get('name')}' (ID: {group_id}) 的映射...")
                    await self.metadata_manager.update_tmdb_mappings(int(tmdb_id), group_id, user)

                    # 步骤 6: 更新作品关联的主剧集组ID
                    await crud.update_anime_tmdb_group_id(session, anime_id, group_id)
                    self.logger.info(f"已将 '{title}' 的主剧集组ID更新为: {group_id}")
                    mapped_count += 1

                    # 步骤 7: 如果使用了剧集组匹配,在中文别名后追加季度信息
                    # 从剧集组名称中提取季度信息
                    group_name = group.get('name', '')
                    season_suffix = None

                    # 尝试从剧集组名称中提取季度
                    # 匹配 "Season 2", "第2季", "S02", "S2" 等格式
                    season_match = re.search(r'(?:season\s+|第|s)(\d+)(?:季)?', group_name, re.IGNORECASE)
                    if season_match:
                        season_num = int(season_match.group(1))
                        # 只为第二季及以后追加季度后缀
                        if season_num >= 2:
                            season_suffix = f" 第{season_num}季"
                            self.logger.info(f"检测到剧集组季度信息: {season_suffix}")

                            # 获取当前别名
                            from ..orm_models import AnimeAlias
                            stmt = select(AnimeAlias).where(AnimeAlias.animeId == anime_id)
                            result = await session.execute(stmt)
                            alias_record = result.scalar_one_or_none()

                            if alias_record:
                                # 为中文别名追加季度后缀
                                updated = False
                                if alias_record.aliasCn1 and not alias_record.aliasCn1.endswith(season_suffix):
                                    alias_record.aliasCn1 = alias_record.aliasCn1 + season_suffix
                                    updated = True
                                if alias_record.aliasCn2 and not alias_record.aliasCn2.endswith(season_suffix):
                                    alias_record.aliasCn2 = alias_record.aliasCn2 + season_suffix
                                    updated = True
                                if alias_record.aliasCn3 and not alias_record.aliasCn3.endswith(season_suffix):
                                    alias_record.aliasCn3 = alias_record.aliasCn3 + season_suffix
                                    updated = True

                                if updated:
                                    await session.commit()
                                    self.logger.info(f"已为 '{title}' 的中文别名追加季度后缀: {season_suffix}")
                        else:
                            self.logger.info(f"检测到第{season_num}季,跳过追加季度后缀(仅第二季及以后追加)")

                await session.commit() # 提交本次节目的所有更改
                processed_count += 1

            except Exception as e:
                self.logger.error(f"处理 '{title}' 时发生错误: {e}", exc_info=True)
                await session.rollback() # 出错时回滚
            finally:
                await asyncio.sleep(1) # 简单的速率限制，防止对TMDB API造成过大压力

        self.logger.info(f"定时任务 [{self.job_name}] 执行完毕。")
        self.logger.info(f"统计: 共处理 {processed_count}/{total_shows} 个作品, 刮削 {scraped_count} 个TMDB ID, 映射 {mapped_count} 个剧集组。")
        # 修正：抛出 TaskSuccess 异常，以便 TaskManager 可以用一个有意义的消息来结束任务
        raise TaskSuccess(f"任务执行完毕，共处理 {processed_count}/{total_shows} 个作品, 刮削 {scraped_count} 个TMDB ID, 映射 {mapped_count} 个剧集组。")