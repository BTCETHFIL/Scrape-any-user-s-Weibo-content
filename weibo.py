#!/usr/bin/env python
# -*- coding: utf-8 -*-

import base64
import codecs
import json
import logging
import logging.config
import math
import os
import random
import re
import sys
import time
import warnings
import webbrowser
from collections import OrderedDict
from datetime import date, datetime, timedelta
from time import sleep

import requests
from requests.exceptions import RequestException
from lxml import etree
from requests.adapters import HTTPAdapter
from tqdm import tqdm

import const
from util.notify import push_deer
from util.llm_analyzer import LLMAnalyzer  # 导入 LLM 分析器

import piexif

warnings.filterwarnings("ignore")

# 如果日志文件夹不存在，则创建
SCRIPT_DIR_WB = os.path.split(os.path.realpath(__file__))[0]
log_dir = os.path.join(SCRIPT_DIR_WB, "log")
if not os.path.isdir(log_dir):
    os.makedirs(log_dir)
logging_path = os.path.join(SCRIPT_DIR_WB, "logging.conf")
_old_cwd = os.getcwd()
os.chdir(SCRIPT_DIR_WB)
try:
    logging.config.fileConfig(logging_path)
finally:
    os.chdir(_old_cwd)
logger = logging.getLogger("weibo")

# 日期时间格式
DTFORMAT = "%Y-%m-%dT%H:%M:%S"

class Weibo(object):
    def __init__(self, config):
        """Weibo类初始化"""
        self.validate_config(config)
        self.only_crawl_original = config["only_crawl_original"]  # 取值范围为0、1,程序默认值为0,代表要爬取用户的全部微博,1代表只爬取用户的原创微博
        self.remove_html_tag = config[
            "remove_html_tag"
        ]  # 取值范围为0、1, 0代表不移除微博中的html tag, 1代表移除
        since_date = config["since_date"]
        # since_date 若为整数，则取该天数之前的日期；若为 yyyy-mm-dd，则增加时间
        if isinstance(since_date, int):
            since_date = date.today() - timedelta(since_date)
            since_date = since_date.strftime(DTFORMAT)
        elif self.is_date(since_date):
            since_date = "{}T00:00:00".format(since_date)
        elif self.is_datetime(since_date):
            pass
        else:
            logger.error("since_date 格式不正确，请确认配置是否正确")
            sys.exit()
        self.since_date = since_date  # 起始时间，即爬取发布日期从该值到现在的微博，形式为yyyy-mm-ddThh:mm:ss，如：2023-08-21T09:23:03
        end_date = config.get("end_date", "")
        # end_date 为空字符串时不限制截止时间
        if end_date:
            if isinstance(end_date, int):
                end_date = date.today() - timedelta(end_date)
                end_date = end_date.strftime(DTFORMAT)
            elif self.is_date(end_date):
                end_date = "{}T23:59:59".format(end_date)
            elif self.is_datetime(end_date):
                pass
            else:
                logger.error("end_date 格式不正确，请确认配置是否正确")
                sys.exit()
        self.end_date = end_date  # 截止时间，为空则不限制
        self.start_page = config.get("start_page", 1)  # 开始爬的页，如果中途被限制而结束可以用此定义开始页码
        self.markdown_split_by = config.get("markdown_split_by", "week")  # markdown文件分割方式，固定为week（按周分组）
        self.original_pic_download = config.get(
            "original_pic_download", 0
        )  # 取值范围为0、1, 0代表不下载原创微博图片,1代表下载
        self.retweet_pic_download = config.get(
            "retweet_pic_download", 0
        )  # 取值范围为0、1, 0代表不下载转发微博图片,1代表下载
        self.user_id_as_folder_name = config.get(
            "user_id_as_folder_name", 0
        )  # 结果目录名，取值为0或1，决定结果文件存储在用户昵称文件夹里还是用户id文件夹里
        self.write_time_in_exif = config.get(
            "write_time_in_exif", 0
        )  # 是否开启微博时间写入EXIF，取值范围为0、1, 0代表不开启, 1代表开启
        self.change_file_time = config.get(
            "change_file_time", 0
        )  # 是否修改文件时间，取值范围为0、1, 0代表不开启, 1代表开启
        self.output_directory = config.get(
            "output_directory", "weibo"
        )  # 输出目录配置，默认为"weibo"
        
        # Cookie支持：优先使用环境变量WEIBO_COOKIE，其次使用config.json中的配置
        cookie_string = os.environ.get("WEIBO_COOKIE") or config.get("cookie")
        if os.environ.get("WEIBO_COOKIE"):
            logger.info("使用环境变量WEIBO_COOKIE中的Cookie")
        
        core_cookies = {}   # 核心包
        backup_cookies = {} # 备份
        # Cookie清洗：提取核心字段。若后续预热失败，则回退使用原版 _T_WM/XSRF-TOKEN
        if cookie_string and "SUB=" in cookie_string:
            # 1. 提取核心 SUB
            match_sub = re.search(r'SUB=(.*?)(;|$)', cookie_string)
            if match_sub:
                core_cookies['SUB'] = match_sub.group(1)
            
            # 2. 提取备份指纹
            match_twm = re.search(r'_T_WM=(.*?)(;|$)', cookie_string)
            if match_twm:
                backup_cookies['_T_WM'] = match_twm.group(1)
            
            match_xsrf = re.search(r'XSRF-TOKEN=(.*?)(;|$)', cookie_string)
            if match_xsrf:
                backup_cookies['XSRF-TOKEN'] = match_xsrf.group(1)
        
        # 保底：如果没有提取到 SUB，说明格式特殊，全量加载
        if not core_cookies and cookie_string:
            for pair in cookie_string.split(';'):
                if '=' in pair:
                    key, value = pair.split('=', 1)
                    core_cookies[key.strip()] = value.strip()
                    
        self.headers = {
            'Referer': 'https://m.weibo.cn/',  # 修正 Referer 为 m.weibo.cn
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
            'cache-control': 'max-age=0',
            'priority': 'u=0, i',
            'sec-ch-ua': '"Chromium";v="136", "Microsoft Edge";v="136", "Not.A/Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'x-requested-with': 'XMLHttpRequest',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0',
        }
        self.page_weibo_count = config.get("page_weibo_count")  # page_weibo_count，爬取一页的微博数，默认10页
        
        # 初始化 LLM 分析器
        self.llm_analyzer = LLMAnalyzer(config) if config.get("llm_config") else None
        
        user_id_list = config["user_id_list"]
        requests_session = requests.Session()
        requests_session.cookies.update(core_cookies)

        self.session = requests_session
        try:
            # 请求只带 SUB
            # 服务器下发适配 m.weibo.cn 的新指纹
            self.session.get("https://m.weibo.cn", headers=self.headers, timeout=10)
            logger.info("Session 预热成功，服务器已下发最新指纹。")
            
        except Exception as e:
            #请求失败时，启用备份
            logger.warning(f"Session 预热失败 ({e})，正在启用备份 Cookie...")
            self.session.cookies.update(backup_cookies) # 把旧指纹装进去救急

        adapter = HTTPAdapter(max_retries=5)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        # 避免卡住
        if isinstance(user_id_list, list):
            random.shuffle(user_id_list)

        query_list = config.get("query_list") or []
        if isinstance(query_list, str):
            query_list = query_list.split(",")
        self.query_list = query_list
        if not isinstance(user_id_list, list):
            if not os.path.isabs(user_id_list):
                user_id_list = (
                    os.path.split(os.path.realpath(__file__))[0] + os.sep + user_id_list
                )
            self.user_config_file_path = user_id_list  # 用户配置文件路径
            user_config_list = self.get_user_config_list(user_id_list)
        else:
            self.user_config_file_path = ""
            user_config_list = [
                {
                    "user_id": user_id,
                    "since_date": self.since_date,
                    "end_date": self.end_date,
                    "query_list": query_list,
                }
                for user_id in user_id_list
            ]

        self.user_config_list = user_config_list  # 要爬取的微博用户的user_config列表
        self.user_config = {}  # 用户配置,包含用户id和since_date
        self.start_date = ""  # 获取用户第一条微博时的日期
        self.query = ""
        self.user = {}  # 存储目标微博用户信息
        self.got_count = 0  # 存储爬取到的微博数
        self.weibo = []  # 存储爬取到的所有微博信息
        self.weibo_id_list = []  # 存储爬取到的所有微博id
        self.long_sleep_count_before_each_user = 0 #每个用户前的长时间sleep避免被ban
        self.captcha_event = None  # GUI 模式下的验证码事件，由外部设置
        self.stop_event = None     # GUI 模式下的停止事件，由外部设置

        # 防封禁配置初始化
        self.anti_ban_config = config.get("anti_ban_config", {})
        self.anti_ban_enabled = self.anti_ban_config.get("enabled", False)

        # 爬取状态跟踪
        self.crawl_stats = {
            "weibo_count": 0,      # 已爬取微博数
            "request_count": 0,    # 已发送请求数
            "api_errors": 0,       # API错误数
            "start_time": None,    # 开始时间
            "batch_count": 0,      # 当前批次计数
            "last_batch_time": None # 上次批次时间
        }
    def calculate_dynamic_delay(self):
        """计算动态延迟时间"""
        if not self.anti_ban_enabled:
            return 0

        config = self.anti_ban_config
        base_delay = config.get("request_delay_min", 8)

        # 根据请求次数增加延迟
        request_count = self.crawl_stats["request_count"]
        if request_count > 100:
            base_delay += 5
        if request_count > 300:
            base_delay += 10

        # 根据爬取时间增加延迟
        if self.crawl_stats["start_time"]:
            time_elapsed = time.time() - self.crawl_stats["start_time"]
            if time_elapsed > 300:  # 5分钟
                base_delay += 5

        # 随机波动
        max_delay = config.get("request_delay_max", 15)
        return random.uniform(base_delay, max_delay)

    def should_pause_session(self):
        """检查是否应该暂停当前会话"""
        if not self.anti_ban_enabled:
            return False, ""

        config = self.anti_ban_config
        current_time = time.time()

        # 条件1：达到数量阈值
        max_weibo = config.get("max_weibo_per_session", 500)
        if self.crawl_stats["weibo_count"] >= max_weibo:
            return True, f"达到单次运行最大微博数({max_weibo})"

        # 条件2：运行时间过长
        if self.crawl_stats["start_time"]:
            session_time = current_time - self.crawl_stats["start_time"]
            max_time = config.get("max_session_time", 600)
            if session_time > max_time:
                return True, f"单次运行时间过长({int(session_time)}秒)"

        # 条件3：API错误率过高
        max_errors = config.get("max_api_errors", 5)
        if self.crawl_stats["api_errors"] >= max_errors:
            return True, f"API错误过多({self.crawl_stats['api_errors']}次)"

        # 条件4：随机概率（模拟用户休息）
        random_prob = config.get("random_rest_probability", 0.01)
        if random.random() < random_prob:
            return True, "随机休息"

        return False, ""

    def check_batch_delay(self):
        """检查是否需要批次延迟"""
        if not self.anti_ban_enabled:
            return

        config = self.anti_ban_config
        batch_size = config.get("batch_size", 50)
        batch_delay = config.get("batch_delay", 30)

        # 检查是否达到批次大小
        if self.crawl_stats["batch_count"] >= batch_size:
            current_time = time.time()

            # 检查距离上次批次的时间
            if self.crawl_stats["last_batch_time"]:
                time_since_last_batch = current_time - self.crawl_stats["last_batch_time"]
                if time_since_last_batch < batch_delay:
                    # 如果距离上次批次时间太短，等待补足
                    wait_time = batch_delay - time_since_last_batch
                    logger.info(f"批次延迟: 等待 {wait_time:.1f} 秒")
                    sleep(wait_time)

            logger.info(f"批次延迟: 等待 {batch_delay} 秒")
            sleep(batch_delay)

            # 重置批次计数
            self.crawl_stats["batch_count"] = 0
            self.crawl_stats["last_batch_time"] = time.time()

    def get_random_headers(self):
        """获取随机请求头"""
        if not self.anti_ban_enabled:
            return self.headers

        config = self.anti_ban_config

        # 随机选择User-Agent
        user_agents = config.get("user_agents", [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0"
        ])
        user_agent = random.choice(user_agents)

        # 随机选择Accept-Language
        accept_languages = config.get("accept_languages", [
            "zh-CN,zh;q=0.9,en;q=0.8"
        ])
        accept_language = random.choice(accept_languages)

        # 随机选择Referer
        referers = config.get("referer_list", [
            "https://m.weibo.cn/",
            "https://weibo.com/"
        ])
        referer = random.choice(referers)

        # 返回随机化的请求头
        return {
            'Referer': referer,
            'accept': 'application/json, text/plain, */*',
            'accept-language': accept_language,
            'cache-control': 'max-age=0',
            'priority': 'u=0, i',
            'sec-ch-ua': '"Chromium";v="136", "Microsoft Edge";v="136", "Not.A/Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'x-requested-with': 'XMLHttpRequest',
            'user-agent': user_agent,
        }

    def update_crawl_stats(self, weibo_count=0, request_count=0, api_error=False):
        """更新爬取统计"""
        if not self.anti_ban_enabled:
            return

        if weibo_count > 0:
            self.crawl_stats["weibo_count"] += weibo_count
            self.crawl_stats["batch_count"] += weibo_count

        if request_count > 0:
            self.crawl_stats["request_count"] += request_count

        if api_error:
            self.crawl_stats["api_errors"] += 1

    def reset_crawl_stats(self):
        """重置爬取统计（休息后调用）"""
        self.crawl_stats = {
            "weibo_count": 0,
            "request_count": 0,
            "api_errors": 0,
            "start_time": time.time(),
            "batch_count": 0,
            "last_batch_time": None
        }
        logger.info("爬取统计已重置，继续爬取")

    def perform_anti_ban_rest(self):
        """执行防封禁休息"""
        if not self.anti_ban_enabled:
            return

        config = self.anti_ban_config
        rest_time_min = config.get("rest_time_min", 600)
        
        # 添加随机波动（±10%）
        rest_time = int(rest_time_min * random.uniform(0.9, 1.1))
        
        logger.info("┌────────────────────────────────────┐")
        logger.info("│ 🛡️ 防封禁休息中...                 │")
        logger.info("│ 休息时间: %-4d 秒                  │", rest_time)
        logger.info("│ 预计恢复: %s       │", 
                   (datetime.now() + timedelta(seconds=rest_time)).strftime("%H:%M:%S"))
        logger.info("└────────────────────────────────────┘")
        
        # 执行休息
        sleep(rest_time)
        
        logger.info("休息结束，继续爬取微博")

    def validate_config(self, config):
        """验证配置是否正确"""

        # 验证如下1/0相关值
        argument_list = [
            "only_crawl_original",
            "original_pic_download",
            "retweet_pic_download",
        ]
        for argument in argument_list:
            # 使用 get() 获取值，新增字段默认为0
            value = config.get(argument, 0)
            if value != 0 and value != 1:
                logger.warning("%s值应为0或1,请重新输入", argument)
                sys.exit()

        # 验证query_list
        query_list = config.get("query_list") or []
        if (not isinstance(query_list, list)) and (not isinstance(query_list, str)):
            logger.warning("query_list值应为list类型或字符串,请重新输入")
            sys.exit()

        # 验证markdown_split_by（仅支持week）
        markdown_split_by = config.get("markdown_split_by", "week")
        if markdown_split_by != "week":
            logger.warning("markdown_split_by仅支持week（按周分组），请修改配置")
            sys.exit()

        # 验证user_id_list
        user_id_list = config["user_id_list"]
        if (not isinstance(user_id_list, list)) and (not user_id_list.endswith(".txt")):
            logger.warning("user_id_list值应为list类型或txt文件路径")
            sys.exit()
        if not isinstance(user_id_list, list):
            if not os.path.isabs(user_id_list):
                user_id_list = (
                    os.path.split(os.path.realpath(__file__))[0] + os.sep + user_id_list
                )
            if not os.path.isfile(user_id_list):
                logger.warning("不存在%s文件", user_id_list)
                sys.exit()

        # 验证since_date
        since_date = config["since_date"]
        if (not isinstance(since_date, int)) and (not self.is_datetime(since_date)) and (not self.is_date(since_date)):
            logger.warning("since_date值应为yyyy-mm-dd形式、yyyy-mm-ddTHH:MM:SS形式或整数，请重新输入")
            sys.exit()

        # 验证end_date
        end_date = config.get("end_date", "")
        if end_date:
            if (not isinstance(end_date, int)) and (not self.is_datetime(end_date)) and (not self.is_date(end_date)):
                logger.warning("end_date值应为yyyy-mm-dd形式、yyyy-mm-ddTHH:MM:SS形式或整数，请重新输入")
                sys.exit()

    def is_datetime(self, since_date):
        """判断日期格式是否为 %Y-%m-%dT%H:%M:%S"""
        try:
            datetime.strptime(since_date, DTFORMAT)
            return True
        except ValueError:
            return False
    
    def is_date(self, since_date):
        """判断日期格式是否为 %Y-%m-%d"""
        try:
            datetime.strptime(since_date, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def get_json(self, params):
        url = "https://m.weibo.cn/api/container/getIndex?"
        try:
            r = self.session.get(url, params=params, headers=self.headers, verify=False, timeout=10)
            r.raise_for_status()
            response_json = r.json()
            return response_json, r.status_code
        except RequestException as e:
            logger.error(f"请求失败，错误信息：{e}")
            return {}, 500
        except ValueError as ve:
            logger.error(f"JSON 解码失败，错误信息：{ve}。响应内容预览：{getattr(r, 'text', '(无)')[:300]}")
            return {}, 500

    def handle_captcha(self, js):
        """
        处理验证码挑战，提示用户手动完成验证。

        参数:
            js (dict): API 返回的 JSON 数据。

        返回:
            bool: 如果用户成功完成验证码，返回 True；否则返回 False。
        """
        logger.debug(f"收到的 JSON 数据：{js}")
        
        captcha_url = js.get("url")
        if captcha_url:
            logger.warning("检测到验证码挑战。正在打开验证码页面以供手动验证。")
            webbrowser.open(captcha_url)
        else:
            logger.warning("检测到可能的验证码挑战，但未提供验证码 URL。请手动检查浏览器并完成验证码验证。")
            return False
        
        logger.info("请在打开的浏览器窗口中完成验证码验证。")
        
        # GUI 模式：使用事件机制等待，避免 input() 阻塞后台线程
        if self.captcha_event is not None:
            self.captcha_event.clear()
            logger.info("等待用户在 GUI 中点击'验证完成'按钮...")
            while True:
                if self.captcha_event.wait(timeout=0.5):
                    logger.info("用户已完成验证码验证，继续爬取。")
                    return True
                if self.stop_event and self.stop_event.is_set():
                    logger.warning("爬取已停止，验证码处理被中断。")
                    return False
            # unreachable
        
        # 命令行模式：使用 input() 等待用户输入
        while True:
            try:
                user_input = input("完成验证码后，请输入 'y' 继续，或输入 'q' 退出：").strip().lower()

                if user_input == 'y':
                    logger.info("用户输入 'y'，继续爬取。")
                    return True
                elif user_input == 'q':
                    logger.warning("用户选择退出，程序中止。")
                    sys.exit("用户选择退出，程序中止。")
                else:
                    logger.warning("无效输入，请重新输入 'y' 或 'q'。")
            except EOFError:
                logger.error("读取用户输入时发生 EOFError，程序退出。")
                sys.exit("输入流已关闭，程序中止。")
    
    def get_weibo_json(self, page):
        """获取网页中微博json数据"""
        url = "https://m.weibo.cn/api/container/getIndex?"
        params = (
            {
                "container_ext": "profile_uid:" + str(self.user_config["user_id"]),
                "containerid": "100103type=401&q=" + self.query,
                "page_type": "searchall",
            }
            if self.query
            else {"containerid": "230413" + str(self.user_config["user_id"])}
        )
        params["page"] = page
        params["count"] = self.page_weibo_count
        max_retries = 5
        retries = 0
        backoff_factor = 5

        while retries < max_retries:
            try:
                # 防封禁：使用随机请求头
                current_headers = self.get_random_headers()

                # 防封禁：动态延迟
                delay = self.calculate_dynamic_delay()
                if delay > 0:
                    logger.debug(f"动态延迟: {delay:.1f} 秒")
                    sleep(delay)

                response = self.session.get(url, params=params, headers=current_headers, timeout=10)
                response.raise_for_status()  # 如果响应状态码不是 200，会抛出 HTTPError
                js = response.json()

                # 更新统计：成功请求
                self.update_crawl_stats(request_count=1)

                if 'data' in js:
                    logger.info(f"成功获取到页面 {page} 的数据。")
                    return js
                else:
                    logger.warning("未能获取到数据，可能需要验证码验证。")
                    if self.handle_captcha(js):
                        logger.info("用户已完成验证码验证，继续请求数据。")
                        retries = 0  # 重置重试计数器
                        continue
                    else:
                        logger.error("验证码验证失败或未完成，程序将退出。")
                        sys.exit()
            except RequestException as e:
                retries += 1
                sleep_time = backoff_factor * (2 ** retries)
                logger.error(f"请求失败，错误信息：{e}。等待 {sleep_time} 秒后重试...")
                sleep(sleep_time)
                # 更新统计：API错误
                self.update_crawl_stats(api_error=True)
            except ValueError as ve:
                retries += 1
                sleep_time = backoff_factor * (2 ** retries)
                resp_preview = response.text[:300] if response is not None and hasattr(response, 'text') else '(无法获取响应内容)'
                logger.error(f"JSON 解码失败，错误信息：{ve}。响应内容预览：{resp_preview}。等待 {sleep_time} 秒后重试...")
                sleep(sleep_time)
                # 更新统计：API错误
                self.update_crawl_stats(api_error=True)

        logger.error("超过最大重试次数，跳过当前页面。")
        return {}
    
    def _init_append_state(self):
        """初始化增量爬取状态"""
        self.last_weibo_id = ""
        self.last_weibo_date = self.user_config["since_date"]

    def get_user_info(self):
        """获取用户信息"""
        params = {"containerid": "100505" + str(self.user_config["user_id"])}
        url = "https://m.weibo.cn/api/container/getIndex"
        
        # 这里在读取下一个用户的时候很容易被ban，需要优化休眠时长
        # 加一个count，不需要一上来啥都没干就sleep
        if self.long_sleep_count_before_each_user > 0:
            sleep_time = random.randint(30, 60)
            # 添加log，否则一般用户不知道以为程序卡了
            logger.info(f"""短暂sleep {sleep_time}秒，避免被ban""")        
            sleep(sleep_time)
            logger.info("sleep结束")  
        self.long_sleep_count_before_each_user = self.long_sleep_count_before_each_user + 1      

        max_retries = 5  # 设置最大重试次数，避免无限循环
        retries = 0
        backoff_factor = 5  # 指数退避的基数（秒）
        
        while retries < max_retries:
            try:
                logger.info(f"准备获取ID：{self.user_config['user_id']}的用户信息第{retries+1}次。")

                # 防封禁：使用随机请求头
                current_headers = self.get_random_headers()

                # 防封禁：动态延迟
                delay = self.calculate_dynamic_delay()
                if delay > 0:
                    logger.debug(f"动态延迟: {delay:.1f} 秒")
                    sleep(delay)

                response = self.session.get(url, params=params, headers=current_headers, timeout=10)
                response.raise_for_status()
                js = response.json()

                # 更新统计：成功请求
                self.update_crawl_stats(request_count=1)
                if 'data' in js and 'userInfo' in js['data']:
                    info = js["data"]["userInfo"]
                    user_info = OrderedDict()
                    user_info["id"] = self.user_config["user_id"]
                    user_info["screen_name"] = info.get("screen_name", "")
                    user_info["gender"] = info.get("gender", "")
                    params = {
                        "containerid": "230283" + str(self.user_config["user_id"]) + "_-_INFO"
                    }
                    zh_list = ["生日", "所在地", "IP属地", "小学", "初中", "高中", "大学", "公司", "注册时间", "阳光信用"]
                    en_list = [
                        "birthday",
                        "location",
                        "ip_location",
                        "education",
                        "education",
                        "education",
                        "education",
                        "company",
                        "registration_time",
                        "sunshine",
                    ]
                    for i in en_list:
                        user_info[i] = ""
                    js, _ = self.get_json(params)
                    if js["ok"]:
                        cards = js["data"]["cards"]
                        if isinstance(cards, list) and len(cards) > 1:
                            card_list = cards[0]["card_group"] + cards[1]["card_group"]
                            for card in card_list:
                                if card.get("item_name") in zh_list:
                                    user_info[
                                        en_list[zh_list.index(card.get("item_name"))]
                                    ] = card.get("item_content", "")
                    user_info["statuses_count"] = self.string_to_int(
                        info.get("statuses_count", 0)
                    )
                    user_info["followers_count"] = self.string_to_int(
                        info.get("followers_count", 0)
                    )
                    user_info["follow_count"] = self.string_to_int(info.get("follow_count", 0))
                    user_info["description"] = info.get("description", "")
                    user_info["profile_url"] = info.get("profile_url", "")
                    user_info["profile_image_url"] = info.get("profile_image_url", "")
                    user_info["avatar_hd"] = info.get("avatar_hd", "")
                    user_info["urank"] = info.get("urank", 0)
                    user_info["mbrank"] = info.get("mbrank", 0)
                    user_info["verified"] = info.get("verified", False)
                    user_info["verified_type"] = info.get("verified_type", -1)
                    user_info["verified_reason"] = info.get("verified_reason", "")
                    self.user = self.standardize_info(user_info)
                    self._init_append_state()
                    logger.info(f"成功获取到用户 {self.user_config['user_id']} 的信息。")
                    return 0
                elif isinstance(js.get("url"), str) and js.get("url").strip():
                    logger.warning("未能获取到用户信息，可能需要验证码验证。")
                    if self.handle_captcha(js):
                        logger.info("用户已完成验证码验证，继续请求用户信息。")
                        retries = 0  # 重置重试计数器
                        continue
                    else:
                        logger.error("验证码验证失败或未完成，程序将退出。")
                        sys.exit()
                elif isinstance(js.get("msg"), str) and "这里还没有内容" in js.get("msg"):
                    logger.warning("未能获取到用户信息，可能账号已注销或用户id有误。")
                    return 1
                else:
                    logger.warning("未能获取到用户信息。")
                    return 1
            except RequestException as e:
                retries += 1
                sleep_time = backoff_factor * (2 ** retries)
                # 如果是JSON解析失败（response存在），记录响应内容
                resp_info = ""
                if 'response' in locals() and hasattr(response, 'text'):
                    resp_info = f" 响应内容预览：{response.text[:300]}"
                logger.error(f"请求失败，错误信息：{e}。{resp_info}等待 {sleep_time} 秒后重试...")
                sleep(sleep_time)
                # 更新统计：API错误
                self.update_crawl_stats(api_error=True)
            except ValueError as ve:
                retries += 1
                sleep_time = backoff_factor * (2 ** retries)
                # 记录响应内容的前500字符，方便定位问题
                resp_preview = response.text[:500] if response is not None and hasattr(response, 'text') else '(无法获取响应内容)'
                logger.error(f"JSON 解码失败，错误信息：{ve}。响应内容预览：{resp_preview}。等待 {sleep_time} 秒后重试...")
                sleep(sleep_time)
                # 更新统计：API错误
                self.update_crawl_stats(api_error=True)
        logger.error("超过最大重试次数，程序将退出。")
        sys.exit("超过最大重试次数，程序已退出。")

    def get_long_weibo(self, id):
        """获取长微博"""
        url = "https://m.weibo.cn/detail/%s" % id
        logger.info(f"""URL: {url} """)
        for i in range(5):
            sleep(random.uniform(1.0, 2.5))
            html = self.session.get(url, headers=self.headers, verify=False).text
            html = html[html.find('"status":') :]
            html = html[: html.rfind('"call"')]
            html = html[: html.rfind(",")]
            html = "{" + html + "}"
            js = json.loads(html, strict=False)
            weibo_info = js.get("status")
            if weibo_info:
                weibo = self.parse_weibo(weibo_info)
                return weibo

    def get_pics(self, weibo_info):
        """获取微博原始图片url"""
        if weibo_info.get("pics"):
            pic_info = weibo_info["pics"]
            pic_list = []
            for pic in pic_info:
                if not isinstance(pic, dict) or not pic.get('large'):
                    continue
                # 跳过视频类型（多视频微博中视频以 type=video 存在 pics 中）
                if pic.get('type') == 'video':
                    continue
                url = pic['large']['url']
                # 将 URL 中的非原图尺寸标识替换为 large，确保获取原图
                url = re.sub(
                    r'/(mw\d+|bmiddle|thumb\d+|orj\d+|woriginal)/',
                    '/large/', url
                )
                pic_list.append(url)
            pics = ",".join(pic_list)
        else:
            pics = ""
        return pics


    def get_live_photo_url(self, weibo_info):
        """获取Live Photo视频URL"""
        live_photo_list = weibo_info.get("live_photo", [])
        return ";".join(live_photo_list) if live_photo_list else ""

    def get_video_url(self, weibo_info):
        """获取微博普通视频URL"""
        video_urls = []
        # 1. 从 pics 中提取多视频（多视频微博中视频以 type=video 存在 pics 中，
        #    视频URL在 videoSrc 字段）
        if weibo_info.get("pics"):
            for pic in weibo_info["pics"]:
                if (isinstance(pic, dict) and pic.get("type") == "video"
                        and pic.get("videoSrc")):
                    video_urls.append(pic["videoSrc"])
        # 2. 如果 pics 中没有视频，回退到 page_info（单视频兼容）
        if not video_urls and weibo_info.get("page_info"):
            if weibo_info["page_info"].get("type") == "video":
                media_info = (weibo_info["page_info"].get("urls")
                             or weibo_info["page_info"].get("media_info"))
                if media_info:
                    url = (media_info.get("mp4_720p_mp4") or
                           media_info.get("mp4_hd_mp4") or
                           media_info.get("mp4_hd_url") or
                           media_info.get("hevc_mp4_hd") or
                           media_info.get("mp4_sd_url") or
                           media_info.get("mp4_ld_mp4") or
                           media_info.get("stream_url_hd") or
                           media_info.get("stream_url"))
                    if url:
                        video_urls.append(url)
        return ";".join(video_urls)

    def write_exif_time(self, file_path, time_str):
        if self.write_time_in_exif:
            """写入 JPG EXIF 元数据"""
            try:
                # 将 "2025-09-06T22:16:36" 转换为 "2025:09:06 22:16:36"
                exif_time = time_str.replace("-", ":").replace("T", " ")[:19]
                exif_dict = {"Exif": {piexif.ExifIFD.DateTimeOriginal: exif_time}}
                exif_bytes = piexif.dump(exif_dict)
                piexif.insert(exif_bytes, file_path)
                logger.debug(f"[EXIF] 已将时间 {exif_time} 写入 {file_path}")
            except Exception as e:
                logger.debug(f"EXIF写入跳过或失败: {e}")

    def set_file_time(self, file_path, time_str):
        if self.change_file_time:
            """修改文件系统时间（修改日期）"""
            try:
                # 兼容带 T 或不带 T 的格式
                clean_time = time_str.replace("T", " ")
                tick = time.mktime(time.strptime(clean_time, "%Y-%m-%d %H:%M:%S"))
                # 同时修改访问时间和修改时间
                os.utime(file_path, (tick, tick))
                logger.debug(f"[FILE] 已将时间 {clean_time} 写入 {file_path}")
            except Exception as e:
                logger.debug(f"修改文件系统时间失败: {e}")

    def download_one_file(self, url, file_path, type, weibo_id, created_at):
        """下载单个文件(图片)"""
        try:

            file_exist = os.path.isfile(file_path)
            need_download = (not file_exist)

            if not need_download:
                return 

            s = requests.Session()
            s.mount('http://', HTTPAdapter(max_retries=2))
            s.mount('https://', HTTPAdapter(max_retries=2))
            try_count = 0
            success = False
            MAX_TRY_COUNT = 3
            detected_extension = None
            # 连续无数据超时时间（秒）：超过此时间没收到任何数据则判定为卡住
            stall_timeout = 60
            while try_count < MAX_TRY_COUNT:
                try:
                    # 使用流式下载，避免大文件一次性加载导致卡住
                    response = s.get(
                        url, headers=self.headers, timeout=(5, 30),
                        verify=False, stream=True
                    )
                    response.raise_for_status()

                    # 流式读取数据，带无数据超时控制
                    # 只要持续收到数据就继续下载，仅在连续 stall_timeout 秒无数据时中断
                    chunks = []
                    last_data_time = time.time()
                    for chunk in response.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            chunks.append(chunk)
                            last_data_time = time.time()  # 收到数据，重置计时
                        # 检查是否长时间无数据
                        if time.time() - last_data_time > stall_timeout:
                            logger.warning(
                                f"下载停滞({stall_timeout}s无数据)，跳过: {url[:80]}..."
                            )
                            raise RequestException(
                                f"下载停滞：连续 {stall_timeout} 秒未收到数据"
                            )
                    downloaded = b''.join(chunks)
                    try_count += 1

                    # 检查下载内容是否为空
                    if not downloaded:
                        logger.warning(f"下载内容为空: {url[:80]}... ({try_count}/{MAX_TRY_COUNT})")
                        continue

                    # 获取文件后缀
                    url_path = url.split('?')[0]  # 去除URL中的参数
                    inferred_extension = os.path.splitext(url_path)[1].lower().strip('.')

                    # 通过 Magic Number 检测文件类型
                    if downloaded.startswith(b'\xFF\xD8\xFF'):
                        # JPEG 文件
                        if not downloaded.endswith(b'\xff\xd9'):
                            logger.debug(f"[DEBUG] JPEG 文件不完整: {url} ({try_count}/{MAX_TRY_COUNT})")
                            continue  # 文件不完整，继续重试
                        detected_extension = '.jpg'
                    elif downloaded.startswith(b'\x89PNG\r\n\x1A\n'):
                        # PNG 文件
                        if not downloaded.endswith(b'IEND\xaeB`\x82'):
                            logger.debug(f"[DEBUG] PNG 文件不完整: {url} ({try_count}/{MAX_TRY_COUNT})")
                            continue  # 文件不完整，继续重试
                        detected_extension = '.png'
                    else:
                        # 其他类型，使用原有逻辑处理
                        if inferred_extension in ['mp4', 'mov', 'webm', 'gif', 'bmp', 'tiff']:
                            detected_extension = '.' + inferred_extension
                        else:
                            # 尝试从 Content-Type 获取扩展名
                            content_type = response.headers.get('Content-Type', '').lower()
                            if 'image/jpeg' in content_type:
                                detected_extension = '.jpg'
                            elif 'image/png' in content_type:
                                detected_extension = '.png'
                            elif 'video/mp4' in content_type:
                                detected_extension = '.mp4'
                            elif 'video/quicktime' in content_type:
                                detected_extension = '.mov'
                            elif 'video/webm' in content_type:
                                detected_extension = '.webm'
                            elif 'image/gif' in content_type:
                                detected_extension = '.gif'
                            else:
                                # 使用原有的扩展名，如果无法确定
                                detected_extension = '.' + inferred_extension if inferred_extension else ''

                    # 动态调整文件路径的扩展名
                    if detected_extension:
                        file_path = re.sub(r'\.\w+$', detected_extension, file_path)

                    # 保存文件
                    if not os.path.isfile(file_path):
                        with open(file_path, "wb") as f:
                            f.write(downloaded)
                            logger.debug("[DEBUG] save " + file_path)
                        if detected_extension in ['.jpg', '.jpeg']:
                            try:
                                self.write_exif_time(file_path, created_at)
                            except Exception as e:
                                logger.error(f"写入EXIF失败: {e}")
                        try:
                            # 1. 无论什么格式，都修改系统时间 (方便文件夹排序)
                            self.set_file_time(file_path, created_at)
                        except Exception as e:
                            logger.error(f"修改文件系统时间失败: {e}")

                    success = True
                    logger.debug("[DEBUG] success " + url + "  " + str(try_count))
                    break  # 下载成功，退出重试循环

                except RequestException as e:
                    try_count += 1
                    logger.error(f"[ERROR] 请求失败，错误信息：{e}。尝试次数：{try_count}/{MAX_TRY_COUNT}")
                    sleep_time = 2 ** try_count  # 指数退避
                    sleep(sleep_time)
                except Exception as e:
                    logger.exception(f"[ERROR] 下载过程中发生错误: {e}")
                    break  # 对于其他异常，退出重试

            if not success:
                logger.debug("[DEBUG] failed " + url + " TOTALLY")
                error_file = self.get_filepath(type) + os.sep + "not_downloaded.txt"
                with open(error_file, "ab") as f:
                    error_entry = f"{weibo_id}:{file_path}:{url}\n"
                    f.write(error_entry.encode(sys.stdout.encoding))
        except Exception as e:
            # 生成原始微博URL
            original_url = f"https://m.weibo.cn/detail/{weibo_id}"
            error_file = self.get_filepath(type) + os.sep + "not_downloaded.txt"
            with open(error_file, "ab") as f:
                error_entry = f"{weibo_id}:{file_path}:{url}:{original_url}\n"
                f.write(error_entry.encode(sys.stdout.encoding))
            logger.exception(e)

    def get_location(self, selector):
        """获取微博发布位置"""
        location_icon = "timeline_card_small_location_default.png"
        span_list = selector.xpath("//span")
        location = ""
        for i, span in enumerate(span_list):
            if span.xpath("img/@src"):
                if location_icon in span.xpath("img/@src")[0]:
                    location = span_list[i + 1].xpath("string(.)")
                    break
        return location

    def get_article_url(self, selector):
        """获取微博中头条文章的url"""
        article_url = ""
        text = selector.xpath("string(.)")
        if text.startswith("发布了头条文章"):
            url = selector.xpath("//a/@data-url")
            if url and url[0].startswith("http://t.cn"):
                article_url = url[0]
        return article_url

    def get_topics(self, selector):
        """获取参与的微博话题"""
        span_list = selector.xpath("//span[@class='surl-text']")
        topics = ""
        topic_list = []
        for span in span_list:
            text = span.xpath("string(.)")
            if len(text) > 2 and text[0] == "#" and text[-1] == "#":
                topic_list.append(text[1:-1])
        if topic_list:
            topics = ",".join(topic_list)
        return topics

    def get_at_users(self, selector):
        """获取@用户"""
        a_list = selector.xpath("//a")
        at_users = ""
        at_list = []
        for a in a_list:
            if "@" + a.xpath("@href")[0][3:] == a.xpath("string(.)"):
                at_list.append(a.xpath("string(.)")[1:])
        if at_list:
            at_users = ",".join(at_list)
        return at_users

    def string_to_int(self, string):
        """字符串转换为整数"""
        if isinstance(string, int):
            return string
        elif string.endswith("万+"):
            string = string[:-2] + "0000"
        elif string.endswith("万"):
            string = float(string[:-1]) * 10000
        elif string.endswith("亿"):
            string = float(string[:-1]) * 100000000
        return int(string)

    def standardize_date(self, created_at):
        """标准化微博发布时间"""
        if "刚刚" in created_at:
            ts = datetime.now()
        elif "分钟" in created_at:
            minute = created_at[: created_at.find("分钟")]
            minute = timedelta(minutes=int(minute))
            ts = datetime.now() - minute
        elif "小时" in created_at:
            hour = created_at[: created_at.find("小时")]
            hour = timedelta(hours=int(hour))
            ts = datetime.now() - hour
        elif "昨天" in created_at:
            day = timedelta(days=1)
            ts = datetime.now() - day
        else:
            created_at = created_at.replace("+0800 ", "")
            ts = datetime.strptime(created_at, "%c")

        created_at = ts.strftime(DTFORMAT)
        full_created_at = ts.strftime("%Y-%m-%d %H:%M:%S")
        return created_at, full_created_at

    def standardize_info(self, weibo):
        """标准化信息，去除乱码"""
        for k, v in weibo.items():
            if (
                "bool" not in str(type(v))
                and "int" not in str(type(v))
                and "list" not in str(type(v))
                and "long" not in str(type(v))
            ):
                weibo[k] = (
                    v.replace("\u200b", "")
                    .encode(sys.stdout.encoding, "ignore")
                    .decode(sys.stdout.encoding)
                )
        return weibo

    def parse_weibo(self, weibo_info):
        weibo = OrderedDict()
        if weibo_info["user"]:
            weibo["user_id"] = weibo_info["user"]["id"]
            weibo["screen_name"] = weibo_info["user"]["screen_name"]
        else:
            weibo["user_id"] = ""
            weibo["screen_name"] = ""
        weibo["id"] = int(weibo_info["id"])
        weibo["bid"] = weibo_info["bid"]
        text_body = weibo_info["text"]
        selector = etree.HTML(f"{text_body}<hr>" if text_body.isspace() else text_body)
        if self.remove_html_tag:
            text_list = selector.xpath("//text()")
            # 若text_list中的某个字符串元素以 @ 或 # 开始，则将该元素与前后元素合并为新元素，否则会带来没有必要的换行
            text_list_modified = []
            for ele in range(len(text_list)):
                if ele > 0 and (text_list[ele-1].startswith(('@','#')) or text_list[ele].startswith(('@','#'))):
                    text_list_modified[-1] += text_list[ele]
                else:
                    text_list_modified.append(text_list[ele])
            weibo["text"] = "\n".join(text_list_modified)
        else:
            weibo["text"] = text_body
        weibo["article_url"] = self.get_article_url(selector)
        weibo["pics"] = self.get_pics(weibo_info)
        weibo["video_url"] = self.get_video_url(weibo_info)  # 普通视频URL
        weibo["live_photo_url"] = self.get_live_photo_url(weibo_info)  # Live Photo视频URL
        weibo["location"] = self.get_location(selector)
        weibo["created_at"] = weibo_info["created_at"]
        weibo["source"] = weibo_info["source"]
        weibo["attitudes_count"] = self.string_to_int(
            weibo_info.get("attitudes_count", 0)
        )
        weibo["comments_count"] = self.string_to_int(
            weibo_info.get("comments_count", 0)
        )
        weibo["reposts_count"] = self.string_to_int(weibo_info.get("reposts_count", 0))
        weibo["topics"] = self.get_topics(selector)
        weibo["at_users"] = self.get_at_users(selector)
        
        # 使用 LLM 分析微博内容
        if self.llm_analyzer:
            weibo = self.llm_analyzer.analyze_weibo(weibo)
            logger.info("完整分析结果：\n%s", json.dumps(weibo, ensure_ascii=False, indent=2))
        return self.standardize_info(weibo)

    def print_user_info(self):
        """打印用户信息"""
        logger.info("+" * 100)
        logger.info("用户信息")
        logger.info("用户id：%s", self.user["id"])
        logger.info("用户昵称：%s", self.user["screen_name"])
        gender = "女" if self.user["gender"] == "f" else "男"
        logger.info("性别：%s", gender)
        logger.info("生日：%s", self.user["birthday"])
        logger.info("所在地：%s", self.user["location"])
        logger.info("IP属地：%s", self.user.get("ip_location", "未获取"))        
        logger.info("教育经历：%s", self.user["education"])
        logger.info("公司：%s", self.user["company"])
        logger.info("阳光信用：%s", self.user["sunshine"])
        logger.info("注册时间：%s", self.user["registration_time"])
        logger.info("微博数：%d", self.user["statuses_count"])
        logger.info("粉丝数：%d", self.user["followers_count"])
        logger.info("关注数：%d", self.user["follow_count"])
        logger.info("url：https://m.weibo.cn/profile/%s", self.user["id"])
        if self.user.get("verified_reason"):
            logger.info(self.user["verified_reason"])
        logger.info(self.user["description"])
        logger.info("+" * 100)

    def print_one_weibo(self, weibo):
        """打印一条微博"""
        try:
            logger.info("微博id：%d", weibo["id"])
            logger.info("微博正文：%s", weibo["text"])
            logger.info("原始图片url：%s", weibo["pics"])
            logger.info("微博位置：%s", weibo["location"])
            logger.info("发布时间：%s", weibo["created_at"])
            logger.info("发布工具：%s", weibo["source"])
            logger.info("点赞数：%d", weibo["attitudes_count"])
            logger.info("评论数：%d", weibo["comments_count"])
            logger.info("转发数：%d", weibo["reposts_count"])
            logger.info("话题：%s", weibo["topics"])
            logger.info("@用户：%s", weibo["at_users"])
            logger.info("已编辑，编辑次数：%d" % weibo.get("edit_count", 0) if weibo.get("edited") else "未编辑")            
            logger.info("url：https://m.weibo.cn/detail/%d", weibo["id"])
        except OSError:
            pass

    def print_weibo(self, weibo):
        """打印微博，若为转发微博，会同时打印原创和转发部分"""
        if weibo.get("retweet"):
            logger.info("*" * 100)
            logger.info("转发部分：")
            self.print_one_weibo(weibo["retweet"])
            logger.info("*" * 100)
            logger.info("原创部分：")
        self.print_one_weibo(weibo)
        logger.info("-" * 120)

    def get_one_weibo(self, info):
        """获取一条微博的全部信息"""
        try:
            weibo_info = info["mblog"]
            weibo_id = weibo_info["id"]
            retweeted_status = weibo_info.get("retweeted_status")
            is_long = (
                True if weibo_info.get("pic_num") > 9 else weibo_info.get("isLongText")
            )
            if retweeted_status and retweeted_status.get("id"):  # 转发
                retweet_id = retweeted_status.get("id")
                is_long_retweet = retweeted_status.get("isLongText")
                if is_long:
                    weibo = self.get_long_weibo(weibo_id)
                    if not weibo:
                        weibo = self.parse_weibo(weibo_info)
                else:
                    weibo = self.parse_weibo(weibo_info)
                if is_long_retweet:
                    retweet = self.get_long_weibo(retweet_id)
                    if not retweet:
                        retweet = self.parse_weibo(retweeted_status)
                else:
                    retweet = self.parse_weibo(retweeted_status)
                (
                    retweet["created_at"],
                    retweet["full_created_at"],
                ) = self.standardize_date(retweeted_status["created_at"])
                weibo["retweet"] = retweet
            else:  # 原创
                if is_long:
                    weibo = self.get_long_weibo(weibo_id)
                    if not weibo:
                        weibo = self.parse_weibo(weibo_info)
                else:
                    weibo = self.parse_weibo(weibo_info)
            weibo["created_at"], weibo["full_created_at"] = self.standardize_date(
                weibo_info["created_at"]
            )
            edit_count = weibo_info.get("edit_count", 0)
            weibo["edited"] = edit_count > 0
            weibo["edit_count"] = edit_count
            return weibo
        except Exception as e:
            logger.exception(e)

    def get_one_page(self, page):
        """获取一页的全部微博"""
        try:
            js = self.get_weibo_json(page)
            if js["ok"]:
                weibos = js["data"]["cards"]
                
                if self.query:
                    weibos = weibos[0]["card_group"]
                # 如果需要检查cookie，在循环第一个人的时候，就要看看仅自己可见的信息有没有，要是没有直接报错
                for w in weibos:
                    if w["card_type"] == 11:
                        temp = w.get("card_group",[0])
                        if len(temp) >= 1:
                            w = temp[0] or w
                        else:
                            w = w
                    if w["card_type"] == 9:
                        wb = self.get_one_weibo(w)
                        if wb:
                            if (
                                const.CHECK_COOKIE["CHECK"]
                                and (not const.CHECK_COOKIE["CHECKED"])
                                and wb["text"].startswith(
                                    const.CHECK_COOKIE["HIDDEN_WEIBO"]
                                )
                            ):
                                const.CHECK_COOKIE["CHECKED"] = True
                                logger.info("cookie检查通过")
                                if const.CHECK_COOKIE["EXIT_AFTER_CHECK"]:
                                    return True
                            if wb["id"] in self.weibo_id_list:
                                continue
                            created_at = datetime.strptime(wb["created_at"], DTFORMAT)
                            since_date = datetime.strptime(
                                self.user_config["since_date"], DTFORMAT
                            )
                            # end_date 过滤：微博按从新到旧排列，晚于截止时间的跳过继续
                            if self.user_config.get("end_date"):
                                end_date = datetime.strptime(
                                    self.user_config["end_date"], DTFORMAT
                                )
                                if created_at > end_date:
                                    # 检查是否为置顶微博
                                    is_pinned = w.get("mblog", {}).get("mblogtype", 0) == 2
                                    if is_pinned:
                                        logger.debug(f"[置顶微博] 微博ID={wb['id']}, 发布时间={created_at}, 是置顶微博，跳过但继续检查后续微博")
                                    else:
                                        logger.debug(f"[截止日期过滤] 微博ID={wb['id']}, 发布时间={created_at}, 截止时间={end_date}, 已跳过")
                                    continue
                            if const.MODE == "append":
                                # append模式：增量获取微博，基于since_date过滤
                                pass  # since_date 过滤在下方的 created_at < since_date 中处理
                            if created_at < since_date:
                                # 检查是否为置顶微博
                                is_pinned = w.get("mblog", {}).get("mblogtype", 0) == 2
                                if is_pinned:
                                    logger.debug(f"[置顶微博] 微博ID={wb['id']}, 发布时间={created_at}, 是置顶微博，跳过但继续检查后续微博")
                                    continue
                                
                                logger.debug(f"[日期过滤] 微博ID={wb['id']}, 发布时间={created_at}, 起始时间={since_date}, 已跳过")
                                # 如果要检查还没有检查cookie，不能直接跳出
                                if const.CHECK_COOKIE["CHECK"] and (
                                    not const.CHECK_COOKIE["CHECKED"]
                                ):
                                    continue
                                else:
                                    logger.info(
                                        "{}已获取{}({})的第{}页{}微博{}".format(
                                            "-" * 30,
                                            self.user["screen_name"],
                                            self.user["id"],
                                            page,
                                            '包含"' + self.query + '"的'
                                            if self.query
                                            else "",
                                            "-" * 30,
                                        )
                                    )
                                    return True
                            else:
                                logger.debug(f"[日期通过] 微博ID={wb['id']}, 发布时间={created_at}, 起始时间={since_date}")
                            if (not self.only_crawl_original) or ("retweet" not in wb.keys()):
                                self.weibo.append(wb)
                                self.weibo_id_list.append(wb["id"])
                                self.got_count += 1

                                # 防封禁：更新微博统计
                                self.update_crawl_stats(weibo_count=1)

                                # 防封禁：检查是否需要暂停
                                if self.anti_ban_enabled:
                                    should_pause, reason = self.should_pause_session()
                                    if should_pause:
                                        logger.warning(f"触发防封禁暂停: {reason}")
                                        return "need_rest"  # 返回特殊值表示需要休息

                                # 这里是系统日志输出，尽量别太杂
                                logger.info(
                                    "已获取用户 {} 的微博，内容为 {}".format(
                                        self.user["screen_name"], wb["text"]
                                    )
                                )
                                # self.print_weibo(wb)
                            else:
                                logger.info("正在过滤转发微博")
                    
                if const.CHECK_COOKIE["CHECK"] and not const.CHECK_COOKIE["CHECKED"]:
                    logger.warning("经检查，cookie无效，系统退出")
                    if const.NOTIFY["NOTIFY"]:
                        push_deer("经检查，cookie无效，系统退出")
                    sys.exit()
            else:
                return True
            logger.info(
                "{}已获取{}({})的第{}页微博{}".format(
                    "-" * 30, self.user["screen_name"], self.user["id"], page, "-" * 30
                )
            )
        except Exception as e:
            logger.exception(e)

    def get_page_count(self):
        """获取微博页数"""
        try:
            weibo_count = self.user["statuses_count"]
            page_weibo_count = self.page_weibo_count
            page_count = int(math.ceil(weibo_count / page_weibo_count))
            if not isinstance(page_weibo_count, int):
                raise ValueError("config.json中每页爬取的微博数 page_weibo_count 必须是一个整数")
            return page_count
        except KeyError:
            logger.exception(
                "程序出错，错误原因可能为以下两者：\n"
                "1.user_id不正确；\n"
                "2.此用户微博可能需要设置cookie才能爬取。\n"
                "解决方案：\n"
                "请参考\n"
                "https://github.com/dataabc/weibo-crawler#如何获取user_id\n"
                "获取正确的user_id；\n"
                "或者参考\n"
                "https://github.com/dataabc/weibo-crawler#3程序设置\n"
                "中的“设置cookie”部分设置cookie信息"
            )

    def get_filepath(self, type):
        """获取结果文件路径"""
        try:
            dir_name = self.user["screen_name"]
            if self.user_id_as_folder_name:
                dir_name = str(self.user_config["user_id"])
            file_dir = (
                os.path.split(os.path.realpath(__file__))[0]
                + os.sep
                + self.output_directory
                + os.sep
                + dir_name
            )
            if type == "img":
                file_dir = file_dir + os.sep + type
            elif type == "markdown":
                # Markdown文件保存在用户目录下，图片在用户目录的img子目录中
                file_dir = file_dir
            if not os.path.isdir(file_dir):
                os.makedirs(file_dir)
            if type == "img":
                return file_dir
            elif type == "markdown":
                # 对于markdown类型，返回目录路径，文件名会在generate_markdown_file中指定
                return file_dir
            return file_dir
        except Exception as e:
            logger.exception(e)


    def update_user_config_file(self, user_config_file_path):
        """更新用户配置文件"""
        with open(user_config_file_path, "rb") as f:
            try:
                lines = f.read().splitlines()
                lines = [line.decode("utf-8-sig") for line in lines]
            except UnicodeDecodeError:
                logger.error("%s文件应为utf-8编码，请先将文件编码转为utf-8再运行程序", user_config_file_path)
                sys.exit()
            for i, line in enumerate(lines):
                info = line.split(" ")
                if len(info) > 0 and info[0].isdigit():
                    if self.user_config["user_id"] == info[0]:
                        if len(info) == 1:
                            info.append(self.user["screen_name"])
                            info.append(self.start_date)
                        if len(info) == 2:
                            info.append(self.start_date)
                        if len(info) > 2:
                            info[2] = self.start_date
                        lines[i] = " ".join(info)
                        break
        with codecs.open(user_config_file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def write_markdown(self, wrote_count):
        """将爬到的信息写入markdown文件"""
        # 按配置分组微博
        weibo_by_group = self.group_weibo_by_config(wrote_count)

        # 先下载图片（如果需要）
        if self.original_pic_download:
            self.download_markdown_images(wrote_count)

        # 为每个分组生成markdown文件（图片 base64 内嵌到 MD 中）
        for group_key, weibo_list in weibo_by_group.items():
            self.generate_markdown_file(group_key, weibo_list)

        # MD 写入完成后删除已嵌入的图片文件
        if self.original_pic_download:
            self._cleanup_markdown_images(wrote_count)

        logger.info("%d条微博写入markdown文件完毕", self.got_count - wrote_count)

    def group_weibo_by_config(self, wrote_count):
        """按配置分组微博"""
        weibo_by_group = {}
        for w in self.weibo[wrote_count:]:
            # 获取微博发布日期（YYYY-MM-DD格式）
            created_at = w.get("created_at", "")
            if not created_at:
                continue

            # 解析日期
            try:
                date_obj = datetime.strptime(created_at, DTFORMAT)
                
                # 按周分组（ISO周）
                iso_year, iso_week, _ = date_obj.isocalendar()
                group_key = f"{iso_year}-W{iso_week:02d}"

                if group_key not in weibo_by_group:
                    weibo_by_group[group_key] = []
                weibo_by_group[group_key].append(w)
            except ValueError:
                logger.warning(f"无法解析微博日期: {created_at}")
                continue

        return weibo_by_group

    def download_markdown_images(self, wrote_count):
        """为Markdown格式下载图片，使用指定的命名规则"""
        # 获取用户目录
        file_dir = self.get_filepath("markdown")
        
        # 所有图片放在同一个 img 目录
        img_dir = os.path.join(file_dir, "img")
        if not os.path.isdir(img_dir):
            os.makedirs(img_dir)

        # 下载图片
        for w in self.weibo[wrote_count:]:
            # 处理原创微博图片
            if w.get("pics"):
                self._download_weibo_images(w, img_dir, is_retweet=False)

            # 处理转发微博图片
            if not self.only_crawl_original and w.get("retweet"):
                retweet = w["retweet"]
                if retweet.get("pics"):
                    self._download_weibo_images(retweet, img_dir, is_retweet=True)

    def _cleanup_markdown_images(self, wrote_count):
        """删除已嵌入 base64 到 MD 中的图片文件，释放磁盘空间"""
        import shutil
        file_dir = self.get_filepath("markdown")
        # 收集本次下载创建的所有 img 目录
        img_dirs = set()
        for w in self.weibo[wrote_count:]:
            created_at = w.get("created_at", "")
            if not created_at:
                continue
            try:
                time_obj = datetime.strptime(created_at, DTFORMAT)
                img_dirs.add(os.path.join(file_dir, "img"))
            except ValueError:
                continue
        for img_dir in img_dirs:
            if os.path.isdir(img_dir):
                try:
                    shutil.rmtree(img_dir)
                    logger.info(f"已清理图片目录: {img_dir}")
                except Exception as e:
                    logger.warning(f"清理图片目录失败: {img_dir} - {e}")

    def _download_weibo_images(self, weibo, img_dir, is_retweet=False):
        """下载单条微博的图片"""
        created_at = weibo.get("created_at", "")
        if not created_at:
            return

        try:
            time_obj = datetime.strptime(created_at, DTFORMAT)
            date_str = time_obj.strftime("%Y-%m-%d")
            time_str = time_obj.strftime("%H:%M:%S")
        except ValueError:
            return

        pics = weibo["pics"].split(",")
        for i, pic_url in enumerate(pics):
            if not pic_url:
                continue

            # 生成图片文件名：YYYY-MM-DD_HH-MM-SS.jpg
            # 如果同一条微博有多张图片，在文件名后加 _1, _2 等后缀
            base_filename = f"{date_str}_{time_str.replace(':', '-')}"
            if len(pics) > 1:
                img_filename = f"{base_filename}_{i+1}.jpg"
            else:
                img_filename = f"{base_filename}.jpg"

            img_path = os.path.join(img_dir, img_filename)

            # 下载图片
            self.download_one_file(pic_url, img_path, "img", weibo["id"], created_at)

    def _embed_image_base64(self, img_path):
        """读取图片文件并返回 base64 内嵌的 Markdown 图片链接。
        根据文件扩展名自动检测 MIME 类型（jpg/png/gif/webp）。
        如果文件不存在或读取失败，返回普通的文件引用链接作为回退。"""
        try:
            if not os.path.isfile(img_path):
                logger.warning(f"图片文件不存在，跳过嵌入: {img_path}")
                return None
            with open(img_path, "rb") as f:
                img_data = f.read()
            if not img_data:
                return None
            import base64 as b64
            b64_str = b64.b64encode(img_data).decode("ascii")
            # 根据扩展名检测 MIME 类型
            ext = os.path.splitext(img_path)[1].lower()
            mime_map = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif',
                '.webp': 'image/webp',
            }
            mime = mime_map.get(ext, 'image/jpeg')
            return f"![image](data:{mime};base64,{b64_str})\n\n"
        except Exception as e:
            logger.warning(f"base64嵌入图片失败: {img_path} - {e}")
            return None

    def generate_markdown_file(self, group_key, weibo_list):
        """生成单个markdown文件（增量模式）"""
        # 获取用户目录
        file_dir = self.get_filepath("markdown")

        # 创建markdown文件路径（按周分组）
        # group_key 格式: "2026-W25"，转为周一起止日期
        parts = group_key.split("-W")
        iso_year, iso_week = int(parts[0]), int(parts[1])
        monday = date.fromisocalendar(iso_year, iso_week, 1)
        sunday = date.fromisocalendar(iso_year, iso_week, 7)
        week_range = f"{monday.strftime('%Y-%m-%d')}_{sunday.strftime('%Y-%m-%d')}"
        md_file_path = os.path.join(file_dir, f"{week_range}.md")
        title_date = f"{iso_year}年第{iso_week}周 ({monday.strftime('%m-%d')} ~ {sunday.strftime('%m-%d')})"
        img_dir = os.path.join(file_dir, "img")

        # 获取用户名
        username = self.user.get("screen_name", "未知用户")

        # 读取已有文件中的微博ID，用于去重（比时间戳更可靠）
        existing_weibo_ids = set()
        existing_content = ""
        if os.path.exists(md_file_path):
            try:
                with open(md_file_path, "r", encoding="utf-8") as f:
                    existing_content = f.read()
                    # 使用正则表达式提取所有 <!-- weibo_id: xxx --> 格式的微博ID
                    weibo_id_pattern = r"<!-- weibo_id: (\d+) -->"
                    matches = re.findall(weibo_id_pattern, existing_content)
                    existing_weibo_ids = set(matches)
                logger.info(f"已读取现有MD文件，包含 {len(existing_weibo_ids)} 条微博记录")
            except Exception as e:
                logger.warning(f"读取现有MD文件失败: {e}，将创建新文件")
                existing_content = ""
                existing_weibo_ids = set()

        # 过滤出新的微博（不在已有文件中的）
        new_weibo_list = []
        for w in weibo_list:
            weibo_id = str(w.get("id", ""))
            if weibo_id and weibo_id not in existing_weibo_ids:
                new_weibo_list.append(w)

        # 如果没有新微博，直接返回
        if not new_weibo_list:
            logger.info(f"分组 {group_key} 没有新微博需要写入")
            return

        # 构建新微博的markdown内容
        new_md_content = ""
        for w in new_weibo_list:
            # 获取时间（HH:MM:SS格式）
            created_at = w.get("created_at", "")
            if not created_at:
                continue

            try:
                time_obj = datetime.strptime(created_at, DTFORMAT)
                time_str = time_obj.strftime("%H:%M:%S")
                date_str = time_obj.strftime("%Y-%m-%d")
                # 按周分组时，显示日期+星期+时间
                weekday_cn = ["一","二","三","四","五","六","日"][time_obj.weekday()]
                heading_time = f"{date_str} 周{weekday_cn} {time_str}"
            except ValueError:
                time_str = "00:00:00"
                date_str = created_at # fallback
                heading_time = created_at

            # 添加时间标题和微博ID（用于增量模式去重）
            weibo_id = w.get("id", "")
            new_md_content += f"### {heading_time}\n<!-- weibo_id: {weibo_id} -->\n"

            # 处理转发微博
            if not self.only_crawl_original and w.get("retweet"):
                # 原创部分
                text = w.get("text", "").strip()
                if text:
                    new_md_content += f"{text}\n\n"

                # 转发部分
                retweet = w["retweet"]
                retweet_text = retweet.get("text", "").strip()
                if retweet_text:
                    new_md_content += f"> 转发: {retweet_text}\n\n"

                # 转发微博图片（图片保存在父微博的月份文件夹中）
                if retweet.get("pics"):
                    pics = retweet["pics"].split(",")
                    # 使用转发微博的时间作为文件名
                    retweet_created_at = retweet.get("created_at", created_at)
                    try:
                        retweet_time_obj = datetime.strptime(retweet_created_at, DTFORMAT)
                        retweet_date_str = retweet_time_obj.strftime("%Y-%m-%d")
                        retweet_time_str = retweet_time_obj.strftime("%H:%M:%S")
                    except ValueError:
                        retweet_date_str = date_str
                        retweet_time_str = time_str

                    for i, pic_url in enumerate(pics):
                        if pic_url:
                            base_filename = f"{retweet_date_str}_{retweet_time_str.replace(':', '-')}"
                            if len(pics) > 1:
                                img_filename = f"{base_filename}_{i+1}.jpg"
                            else:
                                img_filename = f"{base_filename}.jpg"
                            img_path = os.path.join(img_dir, img_filename)
                            embedded = self._embed_image_base64(img_path)
                            if embedded:
                                new_md_content += embedded
                            else:
                                new_md_content += f"![image](img/{img_filename})\n\n"
            else:
                # 原创微博
                text = w.get("text", "").strip()
                if text:
                    new_md_content += f"{text}\n\n"

                # 原创微博图片
                if w.get("pics"):
                    pics = w["pics"].split(",")
                    for i, pic_url in enumerate(pics):
                        if pic_url:
                            base_filename = f"{date_str}_{time_str.replace(':', '-')}"
                            if len(pics) > 1:
                                img_filename = f"{base_filename}_{i+1}.jpg"
                            else:
                                img_filename = f"{base_filename}.jpg"
                            img_path = os.path.join(img_dir, img_filename)
                            embedded = self._embed_image_base64(img_path)
                            if embedded:
                                new_md_content += embedded
                            else:
                                new_md_content += f"![image](img/{img_filename})\n\n"

            # 添加分隔线
            new_md_content += "---\n\n"

        # 写入文件（增量模式）
        try:
            if existing_content:
                # 追加到已有内容末尾
                final_content = existing_content.rstrip() + "\n\n" + new_md_content
            else:
                # 创建新文件，添加标题
                final_content = f"## {title_date} [{username}] 微博存档\n\n" + new_md_content

            with open(md_file_path, "w", encoding="utf-8") as f:
                f.write(final_content)
            logger.info(f"Markdown文件已更新: {md_file_path}，新增 {len(new_weibo_list)} 条微博")
        except Exception as e:
            logger.error(f"生成Markdown文件失败: {e}")

    def write_data(self, wrote_count):
        """将爬到的信息写入markdown文件"""
        if self.got_count > wrote_count:
            self.write_markdown(wrote_count)

    def crawl_single_weibo_url(self, weibo_url: str):
        """
        手动给定微博链接 → 保存单条微博为 MD

        支持格式:
            - https://weibo.com/{uid}/{mid}           (PC端)
            - https://m.weibo.cn/detail/{weibo_id}    (移动端)
            - https://weibo.com/{uid}/{mid}?xxx       (带参数)
            - https://m.weibo.cn/status/{weibo_id}    (分享链接)

        返回 {success: bool, md_path: str, error: str}
        """
        # 解析 URL 提取微博 ID
        weibo_id_numeric = ""
        # 移动端链接: /detail/{id} 或 /status/{id}
        m_detail = re.search(r'm\.weibo\.cn/(?:detail|status)/(\d+)', weibo_url)
        if m_detail:
            weibo_id_numeric = m_detail.group(1)
        else:
            # PC端链接: /{uid}/{mid}
            m_pc = re.search(r'weibo\.com/(\d+)/([A-Za-z0-9]+)', weibo_url)
            if m_pc:
                mid = m_pc.group(2)
                # 将 base62 mid 转换为数字 ID
                weibo_id_numeric = self._mid_to_id(mid)

        if not weibo_id_numeric:
            return {'success': False, 'md_path': '', 'error': f'无法从链接中提取微博ID: {weibo_url}'}

        logger.info(f"手动保存单条微博: {weibo_url} → id={weibo_id_numeric}")

        try:
            # ── 通过 API 获取微博数据 ──
            api_url = f"https://m.weibo.cn/statuses/show?id={weibo_id_numeric}"
            headers = self.get_random_headers()
            response = self.session.get(api_url, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()

            if not data or data.get('ok') != 1:
                err_msg = data.get('msg', '未知API错误') if isinstance(data, dict) else 'API返回异常'
                return {'success': False, 'md_path': '', 'error': f'微博API返回错误: {err_msg}'}

            # ── 解析微博内容 ──
            weibo_data = data.get('data', {})
            if not weibo_data:
                return {'success': False, 'md_path': '', 'error': '微博数据为空（可能已删除或不可见）'}

            info = {'mblog': weibo_data}
            # 获取用户信息
            user_info = weibo_data.get('user', {})
            screen_name = user_info.get('screen_name', '未知用户')
            uid = str(user_info.get('id', ''))

            # 临时设置 self.user_config 和 self.user 以便后续方法可用
            old_user_config = dict(self.user_config) if self.user_config else {}
            old_user = dict(self.user) if self.user else {}
            self.user_config = {'user_id': uid}
            self.user = {'screen_name': screen_name, 'id': uid}

            try:
                weibo = self.get_one_weibo(info)
                if not weibo:
                    return {'success': False, 'md_path': '', 'error': '解析微博内容失败'}
            finally:
                # 恢复原始配置
                self.user_config = old_user_config
                self.user = old_user

            # ── 构建 MD 内容 ──
            created_at = weibo.get('created_at', '')
            full_created_at = weibo.get('full_created_at', '')
            try:
                time_obj = datetime.strptime(created_at, DTFORMAT)
                date_str = time_obj.strftime('%Y-%m-%d')
                time_str = time_obj.strftime('%H:%M:%S')
                heading = f"{date_str} {time_str}"
            except (ValueError, TypeError):
                heading = created_at or '未知时间'
                date_str = heading[:10] if len(heading) >= 10 else heading
                time_str = ''

            lines = []
            lines.append(f"# 单条微博存档 - {screen_name}")
            lines.append("")
            lines.append(f"**{screen_name}** / {heading}")
            lines.append("")
            lines.append(f"> 🔗 原文链接: [https://weibo.com/{uid}/{weibo.get('bid', weibo_id_numeric)}]({weibo_url})")
            lines.append(f"> 🕒 保存时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            ed_count = weibo.get('edit_count', 0)
            if ed_count > 0:
                lines.append(f"> ✏ 已编辑 ({ed_count}次)")
            lines.append("")

            # 微博正文
            text = weibo.get('text', '').strip()
            if text:
                lines.append("## 📝 微博正文")
                lines.append("")
                lines.append(text)
                lines.append("")

            # 转发内容
            retweet = weibo.get('retweet')
            if retweet:
                lines.append("> ---")
                rt_text = retweet.get('text', '').strip()
                if rt_text:
                    lines.append(f"> **转发内容:** {rt_text}")
                    lines.append(">")

            # 图片 base64 嵌入
            pics = weibo.get('pics', '')
            if pics:
                lines.append("## 🖼 图片")
                lines.append("")
                pic_list = pics.split(',')
                for j, pic_url in enumerate(pic_list):
                    if not pic_url or not pic_url.strip():
                        continue
                    pic_url = pic_url.strip()
                    try:
                        img_resp = self.session.get(pic_url, headers=headers, timeout=15)
                        if img_resp.status_code == 200 and img_resp.content:
                            img_b64 = base64.b64encode(img_resp.content).decode('ascii')
                            # 检测 MIME 类型
                            content_type = img_resp.headers.get('content-type', '')
                            if 'png' in content_type:
                                mime = 'image/png'
                            elif 'gif' in content_type:
                                mime = 'image/gif'
                            elif 'webp' in content_type:
                                mime = 'image/webp'
                            else:
                                mime = 'image/jpeg'
                            lines.append(f"![图片{j+1}](data:{mime};base64,{img_b64})")
                            lines.append("")
                    except Exception as e:
                        logger.warning(f"下载图片失败: {pic_url} - {e}")
                        lines.append(f"![图片{j+1}]({pic_url})")
                        lines.append("")

            lines.append("---")
            lines.append("")

            md_text = '\n'.join(lines)

            # ── 保存 MD ──
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', screen_name)
            safe_date = date_str.replace('-', '')
            wid_short = str(weibo_id_numeric)[:8]
            md_filename = f"单条_{safe_name}_{safe_date}_{wid_short}.md"

            # 输出到 weibo_data/manual/{screen_name}/ 子目录
            file_dir = os.path.join(SCRIPT_DIR_WB, "weibo_data", "manual", safe_name)
            os.makedirs(file_dir, exist_ok=True)
            md_path = os.path.join(file_dir, md_filename)
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(md_text)

            logger.info(f"单条微博已保存: {md_path}")
            return {'success': True, 'md_path': md_path, 'error': ''}

        except requests.exceptions.RequestException as e:
            return {'success': False, 'md_path': '', 'error': f'网络请求失败: {e}'}
        except Exception as e:
            logger.exception(f"手动保存微博失败: {e}")
            return {'success': False, 'md_path': '', 'error': str(e)}

    # ── 微博 MID ↔ ID 转换 (URL 短字符串 ↔ 纯数字) ──
    BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    @staticmethod
    def _base62_decode(s: str) -> int:
        """base62 字符串 → 整数"""
        num = 0
        for ch in s:
            num = num * 62 + Weibo.BASE62.index(ch)
        return num

    @staticmethod
    def _base62_encode(num: int) -> str:
        """整数 → base62 字符串"""
        if num == 0:
            return Weibo.BASE62[0]
        chars = []
        while num > 0:
            chars.append(Weibo.BASE62[num % 62])
            num //= 62
        return ''.join(reversed(chars))

    @staticmethod
    def _mid_to_id(mid: str) -> str:
        """将微博 URL 短字符串 (mid) 解码为纯数字 ID

        算法: mid 是数字 ID 的分组 base62 编码结果。
        数字 ID → 逆序 → 每 7 位一组 → 各组 base62 编码(4字符) → 拼接后逆序 = mid
        解码即反过程: mid 逆序 → 每 4 字符一组 → base62 解码 → 左补零至 7 位 → 逆序拼接
        """
        s = mid[::-1]  # 逆序
        size = len(s) // 4 if len(s) % 4 == 0 else len(s) // 4 + 1
        result = []
        for i in range(size):
            chunk = s[i * 4:(i + 1) * 4][::-1]  # 取 4 字符再逆序还原
            num_str = str(Weibo._base62_decode(chunk))
            if i < size - 1 and len(num_str) < 7:
                num_str = '0' * (7 - len(num_str)) + num_str
            result.append(num_str)
        result.reverse()
        return ''.join(result)

    def get_pages(self):
        """获取全部微博"""
        try:
            # 用户id不可用
            if self.get_user_info() != 0:
                return
            logger.info("准备搜集 {} 的微博".format(self.user["screen_name"]))

            # 防封禁：初始化爬取统计
            if self.anti_ban_enabled:
                self.crawl_stats["start_time"] = time.time()
                cfg = self.anti_ban_config
                logger.info("🛡️ 防封禁模式已启用")
                logger.info("┌────────────────────────────────────┐")
                logger.info("│ 每会话最大微博数: %-17d│", cfg['max_weibo_per_session'])
                logger.info("│ 批次大小: %-8d 批次延迟: %3d秒 │", cfg['batch_size'], cfg['batch_delay'])
                logger.info("│ 请求延迟: %d-%d秒                   │", cfg['request_delay_min'], cfg['request_delay_max'])
                logger.info("│ 最大会话时间: %-7d秒            │", cfg['max_session_time'])
                logger.info("│ 最大API错误数: %-20d│", cfg['max_api_errors'])
                logger.info("└────────────────────────────────────┘")

            since_date = datetime.strptime(self.user_config["since_date"], DTFORMAT)
            today = datetime.today()
            if since_date <= today:    # since_date 若为未来则无需执行
                page_count = self.get_page_count()
                wrote_count = 0
                page1 = 0
                random_pages = random.randint(1, 5)
                self.start_date = datetime.now().strftime(DTFORMAT)
                pages = range(self.start_page, page_count + 1)
                for page in tqdm(pages, desc="Progress"):
                    is_end = self.get_one_page(page)
                    
                    # 防封禁：检查是否需要休息
                    if is_end == "need_rest":
                        # 先写入已爬取的数据
                        self.write_data(wrote_count)
                        wrote_count = self.got_count
                        
                        # 执行休息
                        self.perform_anti_ban_rest()
                        
                        # 重置统计，继续爬取
                        self.reset_crawl_stats()
                        continue
                    
                    if is_end:
                        break

                    # 防封禁：检查批次延迟
                    if self.anti_ban_enabled:
                        self.check_batch_delay()

                    if page % 20 == 0:  # 每爬20页写入一次文件
                        self.write_data(wrote_count)
                        wrote_count = self.got_count

                    # 防封禁：保留原有延迟逻辑，但可根据配置调整
                    if self.anti_ban_enabled:
                        # 如果启用了防封禁，使用更保守的延迟
                        if (page - page1) % random_pages == 0 and page < page_count:
                            delay = random.randint(8, 12)  # 更保守的延迟
                            sleep(delay)
                            page1 = page
                            random_pages = random.randint(1, 5)
                    else:
                        # 原有逻辑
                        if (page - page1) % random_pages == 0 and page < page_count:
                            sleep(random.randint(6, 10))
                            page1 = page
                            random_pages = random.randint(1, 5)

                self.write_data(wrote_count)  # 将剩余不足20页的微博写入文件

            # 防封禁：输出统计信息
            if self.anti_ban_enabled:
                session_time = time.time() - self.crawl_stats["start_time"]
                logger.info(f"防封禁统计: 微博={self.crawl_stats['weibo_count']}, 请求={self.crawl_stats['request_count']}, 错误={self.crawl_stats['api_errors']}, 耗时={int(session_time)}秒")

            logger.info("微博爬取完成，共爬取%d条微博", self.got_count)
        except Exception as e:
            logger.exception(e)

    def get_user_config_list(self, file_path):
        """获取文件中的微博id信息"""
        with open(file_path, "rb") as f:
            try:
                lines = f.read().splitlines() 
                lines = [line.decode("utf-8-sig") for line in lines]
            except UnicodeDecodeError:
                logger.error("%s文件应为utf-8编码，请先将文件编码转为utf-8再运行程序", file_path)
                sys.exit()
            user_config_list = []
            # 分行解析配置，添加到user_config_list
            for line in lines:
                info = line.strip().split(" ")    # 去除字符串首尾空白字符
                if len(info) > 0 and info[0].isdigit():
                    user_config = {}
                    user_config["user_id"] = info[0]
                    # 根据配置文件行的字段数确定 since_date 的值
                    if len(info) == 3:
                        if self.is_datetime(info[2]):
                            user_config["since_date"] = info[2]
                        elif self.is_date(info[2]):
                            user_config["since_date"] = "{}T00:00:00".format(info[2])
                        elif info[2].isdigit():
                            since_date = date.today() - timedelta(int(info[2]))
                            user_config["since_date"] = since_date.strftime(DTFORMAT)
                        else:
                            logger.error("since_date 格式不正确，请确认配置是否正确")
                            sys.exit()
                        logger.info(f"用户 {user_config['user_id']} 使用文件中的起始时间: {user_config['since_date']}")
                    else:
                        user_config["since_date"] = self.since_date
                        logger.info(f"用户 {user_config['user_id']} 使用配置文件的起始时间: {user_config['since_date']}")
                    # end_date 统一使用全局配置
                    user_config["end_date"] = self.end_date
                    # 若超过3个字段，则第四个字段为 query_list                    
                    if len(info) > 3:
                        user_config["query_list"] = info[3].split(",")
                    else:
                        user_config["query_list"] = self.query_list
                    if user_config not in user_config_list:
                        user_config_list.append(user_config)
        return user_config_list

    def initialize_info(self, user_config):
        """初始化爬虫信息"""
        self.weibo = []
        self.user = {}
        self.user_config = user_config
        self.got_count = 0
        self.weibo_id_list = []

    def start(self):
        """运行爬虫"""
        try:
            for user_config in self.user_config_list:
                if len(user_config["query_list"]):
                    for query in user_config["query_list"]:
                        self.query = query
                        self.initialize_info(user_config)
                        self.get_pages()
                else:
                    self.initialize_info(user_config)
                    self.get_pages()

                logger.info("信息抓取完毕")
                logger.info("*" * 100)
                if self.user_config_file_path and self.user:
                    self.update_user_config_file(self.user_config_file_path)
        except Exception as e:
            logger.exception(e)


def handle_config_renaming(config, oldName, newName):
    if oldName in config and newName not in config:
        config[newName] = config[oldName]
        del config[oldName]

def get_config():
    """获取配置文件信息"""
    config_path = os.path.split(os.path.realpath(__file__))[0] + os.sep + "config.json"
    if not os.path.isfile(config_path):
        logger.warning(
            "当前路径：%s 不存在配置文件config.json",
            (os.path.split(os.path.realpath(__file__))[0] + os.sep),
        )
        sys.exit()
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)

            # 重命名一些key, 但向前兼容
            handle_config_renaming(config, oldName="filter", newName="only_crawl_original")
            handle_config_renaming(config, oldName="result_dir_name", newName="user_id_as_folder_name")
            return config
    except Exception as e:
        logger.error(f"读取配置文件失败: {e}")
        logger.error("请确保config.json存在且格式正确")
        sys.exit()


def main():
    try:
        config = get_config()
        wb = Weibo(config)
        wb.start()  # 爬取微博信息
        if const.NOTIFY["NOTIFY"]:
            push_deer("更新了一次微博")
    except Exception as e:
        if const.NOTIFY["NOTIFY"]:
            push_deer("weibo-crawler运行出错，错误为{}".format(e))
        logger.exception(e)


if __name__ == "__main__":
    main()
