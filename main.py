import os
import re
import json
import time
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api import logger

# 敏感词配置
SENSITIVE_WORDS = {}
CONFIG_FILE = None
VIOLATION_RECORDS_FILE = None  # 违规记录持久化文件

# 审核配置（全局，供模块级函数使用）
WORD_WHITELIST = []
STRICT_MODE = False

# 违规记录存储（内存，同步持久化到文件）
# 格式: {user_id: {"warnings": [时间戳列表], "blocks": [时间戳列表]}}
VIOLATION_RECORDS = {}

# 违规详情记录（用于WebUI展示）
VIOLATION_DETAILS = []

# 黑名单存储
BLACKLIST = set()

# Token 用量持久化
TOKEN_USAGE_FILE = None  # 在 __init__ 中初始化
TOKEN_USAGE = {
    "total_calls": 0,
    "total_prompt_tokens": 0,    # 估算的输入 token
    "total_completion_tokens": 0, # 估算的输出 token
    "today_prompt_tokens": 0,
    "today_completion_tokens": 0,
    "today_calls": 0,
    "last_reset_date": "",       # 上次日重置的日期
    # 消息统计
    "total_messages": 0,         # 累计审核消息数
    "total_chars": 0,            # 累计审核文字数
    "daily_messages": 0,         # 今日审核消息数
    "daily_chars": 0,            # 今日审核文字数
    "monthly_messages": 0,       # 本月审核消息数
    "monthly_chars": 0,          # 本月审核文字数
    "monthly_date": "",          # 上次月重置的日期（格式 YYYY-MM）
    "daily_history": {},         # 历史日快照 { "2026-07-22": {"calls":..., "messages":...} }
    "monthly_history": {},       # 历史月快照 { "2026-07": {"calls":..., "messages":...} }
}

# 常见模型定价（每 1M token 的美元价格）
MODEL_PRICES = {
    "deepseek-chat": {"input": 0.27, "output": 1.10, "name": "DeepSeek V3", "url": "https://platform.deepseek.com/api-docs/pricing"},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19, "name": "DeepSeek R1", "url": "https://platform.deepseek.com/api-docs/pricing"},
    "gpt-4o": {"input": 2.50, "output": 10.00, "name": "GPT-4o", "url": "https://openai.com/api/pricing/"},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "name": "GPT-4o-mini", "url": "https://openai.com/api/pricing/"},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00, "name": "Claude 3.5 Sonnet", "url": "https://www.anthropic.com/pricing"},
    "claude-3-haiku": {"input": 0.25, "output": 1.25, "name": "Claude 3 Haiku", "url": "https://www.anthropic.com/pricing"},
    "qwen-max": {"input": 2.00, "output": 6.00, "name": "通义千问 Max", "url": "https://help.aliyun.com/zh/model-studio/getting-started/models"},
    "qwen-plus": {"input": 0.80, "output": 2.00, "name": "通义千问 Plus", "url": "https://help.aliyun.com/zh/model-studio/getting-started/models"},
    "glm-4": {"input": 1.00, "output": 1.00, "name": "GLM-4", "url": "https://open.bigmodel.cn/pricing"},
    "custom": {"input": 0.50, "output": 1.50, "name": "自定义", "url": ""},
}

# 歧义词库（单独使用时正常，仅在特定语境组合下才违规的词）
# 这些词绝不能单独被加入违禁词库，否则会造成大面积误判
# 例如 "香草" 单独是香草味，只有 "香草萝莉" 才违规
AMBIGUOUS_WORDS = {
    # 谐音/隐晦动词词根（需配合上下文才有违规含义）
    '香草', '大调查', '上', '草', '搞', '干', '约',
    # 常见动词/形容词/量词（单独无违规含义）
    '看', '要', '想', '大', '来', '去', '做', '弄', '弄一下',
    # 单独使用正常的名词（特定语境才违规）
    '萝莉', '正太', '妹妹', '姐姐', '学生', '老师',
    # 其他容易误判的短词
    '日', '插', '摸', '吸', '舔', '吹', '揉',
}

def load_sensitive_words():
    """加载敏感词"""
    global SENSITIVE_WORDS
    if not CONFIG_FILE:
        logger.error("[文本审核] 配置文件路径未初始化")
        SENSITIVE_WORDS = {"blocked": [], "warning": []}
        return
    
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            SENSITIVE_WORDS = json.load(f)
        logger.info(f"[文本审核] 敏感词加载完成: 封禁词 {len(SENSITIVE_WORDS.get('blocked', []))} 个, 警告词 {len(SENSITIVE_WORDS.get('warning', []))} 个")
    except Exception as e:
        logger.error(f"[文本审核] 加载敏感词失败: {e}")
        SENSITIVE_WORDS = {"blocked": [], "warning": []}

def check_sensitive_words(text):
    """
    检测文本中的敏感词
    返回: {"blocked": [匹配的封禁词], "warning": [匹配的警告词], "has_sensitive": True/False}
    """
    result = {"blocked": [], "warning": [], "has_sensitive": False}
    
    if not SENSITIVE_WORDS:
        return result
    
    def is_match(word, text):
        """检查敏感词是否匹配文本"""
        # 先检查是否在白名单中
        word_lower = word.lower()
        if word_lower in WORD_WHITELIST:
            return False

        # 运行时安全网：跳过歧义词（即使误混进词库也不匹配）
        # 这些词单独使用正常，只有特定组合才违规，靠 AI 每次判断
        if word in AMBIGUOUS_WORDS:
            return False

        if STRICT_MODE:
            # 严格模式：只匹配完整词语（不匹配词中词）
            # 使用边界匹配，只在词边界处匹配
            pattern = r'\b' + re.escape(word) + r'\b'
            return bool(re.search(pattern, text))
        else:
            # 非严格模式：匹配词中词
            return word in text
    
    # 检测封禁词
    for word in SENSITIVE_WORDS.get('blocked', []):
        if is_match(word, text):
            result['blocked'].append(word)
    
    # 检测警告词
    for word in SENSITIVE_WORDS.get('warning', []):
        if is_match(word, text):
            result['warning'].append(word)
    
    result['has_sensitive'] = len(result['blocked']) > 0 or len(result['warning']) > 0
    return result

class TextModerationPlugin(Star):
    """文本审核插件"""
    
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        # 保存配置
        self.config = config if config is not None else {}
        
        # 初始化敏感词配置
        global CONFIG_FILE, VIOLATION_RECORDS_FILE, TOKEN_USAGE_FILE
        # 使用self.name，如果不可用则使用硬编码的插件名
        plugin_name = getattr(self, 'name', 'astrbot_plugin_text_moderation')
        plugin_data_path = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        CONFIG_FILE = str(plugin_data_path / 'sensitive_words.json')
        VIOLATION_RECORDS_FILE = str(plugin_data_path / 'violation_records.json')
        TOKEN_USAGE_FILE = str(plugin_data_path / 'token_usage.json')
        
        # 确保数据目录存在
        plugin_data_path.mkdir(parents=True, exist_ok=True)
        
        # 加载持久化的 token 用量
        self._load_token_usage()
        
        # 如果配置文件不存在，创建默认配置
        if not os.path.exists(CONFIG_FILE):
            default_words = {
                "blocked": [
                    # 直接违规词
                    "傻逼", "操你妈", "草泥马", "去死", "脑残", "废物",
                    "骗子", "诈骗", "违法", "犯罪", "色情", "毒品",
                    "恐怖", "暴力", "血腥", "杀人", "抢劫", "盗窃",
                    "涉政", "反动", "邪教", "赌博", "贩毒",
                    # 谐音/拆字违规词（几乎只有违规含义，不会误判）
                    "弓虽女干", "约炮", "大屌",
                    "草萝莉", "上萝莉", "搞萝莉", "干萝莉",
                    "想草", "想操",
                    "强*奸", "强.奸", "强_奸",
                    "qiangjian", "rape",
                    "发伦功", "法lun功"
                ],
                "warning": [
                    # 直接辱骂
                    "你妈", "傻子", "笨蛋", "蠢货", "神经病", "智障",
                    "该死", "讨厌", "烦人", "有病", "混蛋", "猥琐",
                    "变态", "垃圾", "滚蛋", "白痴", "弱智", "脑残",
                    # 明确人身攻击（几乎只有负面含义的短语）
                    "你脑子有问题", "你脑子有病", "你智商有问题",
                    "你也配", "就你也配", "你算什么东西",
                    "你怕不是个傻子", "你怕不是有病",
                    "就你这智商", "就你这水平"
                ]
            }
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_words, f, ensure_ascii=False, indent=2)
            logger.info(f"[文本审核] 创建默认敏感词配置文件: {CONFIG_FILE}")
        
        # 加载敏感词
        load_sensitive_words()
        
        # 加载持久化的违规记录
        self.load_violation_records()
        
        # 加载插件配置
        self.load_config()
        
        logger.info("[文本审核插件] 已加载")
    
    def load_config(self):
        """加载插件配置"""
        # 读取插件配置
        self.ai_provider = self.config.get('ai_provider', '')
        self.enable_auto_moderation = self.config.get('enable_auto_moderation', True)
        self.enable_keyword_moderation = self.config.get('enable_keyword_moderation', True)
        self.enable_ai_moderation = self.config.get('enable_ai_moderation', True)
        self.enable_intercept_message = self.config.get('enable_intercept_message', True)
        self.min_message_length = self.config.get('min_message_length', 2)
        allowed_groups = self.config.get('allowed_groups', [])
        # 确保群号都是字符串类型
        self.allowed_groups = [str(g) for g in allowed_groups] if allowed_groups else []
        
        # 敏感词白名单和严格模式
        word_whitelist = self.config.get('word_whitelist', [])
        self.word_whitelist = [w.lower() for w in word_whitelist] if word_whitelist else []
        self.strict_mode = self.config.get('strict_mode', False)
        
        # 同步到全局变量，供模块级函数使用
        global WORD_WHITELIST, STRICT_MODE
        WORD_WHITELIST = self.word_whitelist
        STRICT_MODE = self.strict_mode
        
        # 量刑管控配置
        self.enable_punishment = self.config.get('enable_punishment', True)
        self.enable_warning_punishment = self.config.get('enable_warning_punishment', True)
        self.enable_block_punishment = self.config.get('enable_block_punishment', True)
        self.enable_blacklist = self.config.get('enable_blacklist', True)
        self.punishment_time_window = self.config.get('punishment_time_window', 60)
        
        # 撤回消息开关
        self.enable_recall_message = self.config.get('enable_recall_message', True)
        
        # AI自动收集违禁词开关
        self.enable_auto_collect = self.config.get('enable_auto_collect', True)

        # 管理员列表和违规通知开关
        admin_list = self.config.get('admin_list', [])
        self.admin_list = [str(a) for a in admin_list] if admin_list else []
        self.enable_admin_notify = self.config.get('enable_admin_notify', False)
        
        # 自动清理词库中的白名单词（防止之前误添加）
        self._clean_whitelist_words()
        # 自动清理词库中的歧义词（防止之前误添加"香草"等有多重含义的词）
        self._clean_ambiguous_words()

        # 警告惩罚规则（轻度违规：通常用禁言）
        self.warning_punishment_count = self.config.get('warning_punishment_count', 3)
        self.warning_punishment_action = self.config.get('warning_punishment_action', 'mute')
        self.warning_punishment_duration = self.config.get('warning_punishment_duration', 30)
        
        # 封禁惩罚规则（严重违规：通常用踢出）
        self.block_punishment_count = self.config.get('block_punishment_count', 2)
        self.block_punishment_action = self.config.get('block_punishment_action', 'kick')
        self.block_punishment_duration = self.config.get('block_punishment_duration', 120)
        
        logger.info(f"[文本审核] 配置加载完成 - AI提供商: {self.ai_provider if self.ai_provider else '默认'}")
        logger.info(f"[文本审核] 审核开关 - 自动审核: {self.enable_auto_moderation}, 关键词审核: {self.enable_keyword_moderation}, AI审核: {self.enable_ai_moderation}, 消息拦截: {self.enable_intercept_message}, 消息撤回: {self.enable_recall_message}, 自动收集违禁词: {self.enable_auto_collect}")
        logger.info(f"[文本审核] 允许的群: {self.allowed_groups if self.allowed_groups else '全部'}, 最小长度: {self.min_message_length}, 白名单: {self.word_whitelist}, 严格模式: {self.strict_mode}")
        logger.info(f"[文本审核] 量刑管控 - 启用: {self.enable_punishment}, 轻度违规警告: {self.enable_warning_punishment}, 严重违规惩罚: {self.enable_block_punishment}, 黑名单: {self.enable_blacklist}, 时间窗口: {self.punishment_time_window}分钟")
        logger.info(f"[文本审核] 轻度违规 - 仅警告并撤回，不计入惩罚次数")
        logger.info(f"[文本审核] 严重违规 - {self.block_punishment_count}次后: {self.block_punishment_action}" + (f" {self.block_punishment_duration}分钟" if self.block_punishment_action == 'mute' else ""))
        logger.info(f"[文本审核] 管理员: {self.admin_list if self.admin_list else '未设置'}, 违规私信通知: {self.enable_admin_notify}")
        
        # 初始化 Web 控制器（直接在插件中注册路由，不依赖外部模块）
        self._register_web_routes()
    
    def _clean_whitelist_words(self):
        """自动从词库中移除白名单词（防止之前误添加）"""
        if not self.word_whitelist:
            return
        try:
            if not os.path.exists(CONFIG_FILE):
                return
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                words_data = json.load(f)
            
            removed = []
            for word_type in ['blocked', 'warning']:
                if word_type not in words_data:
                    continue
                original = words_data[word_type]
                cleaned = []
                for w in original:
                    w_lower = w.lower()
                    is_whitelisted = False
                    for wl in self.word_whitelist:
                        if wl.lower() in w_lower or w_lower == wl.lower():
                            is_whitelisted = True
                            break
                    if is_whitelisted:
                        removed.append(w)
                    else:
                        cleaned.append(w)
                words_data[word_type] = cleaned
            
            if removed:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(words_data, f, ensure_ascii=False, indent=2)
                load_sensitive_words()
                logger.info(f"[文本审核] 已自动清理词库中的白名单词: {removed}")
        except Exception as e:
            logger.error(f"[文本审核] 清理白名单词失败: {e}")

    def _clean_ambiguous_words(self):
        """自动从词库中移除歧义词（可能误添加的有多重含义的词）"""
        try:
            if not os.path.exists(CONFIG_FILE):
                return
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                words_data = json.load(f)

            removed = []
            for word_type in ['blocked', 'warning']:
                if word_type not in words_data:
                    continue
                original = words_data[word_type]
                cleaned = []
                for w in original:
                    if w in AMBIGUOUS_WORDS:
                        removed.append(w)
                    else:
                        cleaned.append(w)
                words_data[word_type] = cleaned

            if removed:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(words_data, f, ensure_ascii=False, indent=2)
                load_sensitive_words()
                logger.info(f"[文本审核] 已自动清理词库中的歧义词（单独使用正常的词）: {removed}")
        except Exception as e:
            logger.error(f"[文本审核] 清理歧义词失败: {e}")
    
    def _wrap_web_handler(self, handler):
        """包装处理函数，设置 __name__ 属性（参考 astrbot_plugin_group_aip_review）"""
        async def wrapped():
            try:
                return await handler()
            except Exception as exc:
                from quart import jsonify
                logger.exception(f"[文本审核] WebAPI 请求失败: {exc}")
                return jsonify({"ok": False, "message": str(exc)}), 500
        wrapped.__name__ = handler.__name__
        return wrapped
    
    def _register_web_routes(self):
        """注册 WebUI API 路由"""
        PLUGIN_NAME = "astrbot_plugin_text_moderation"
        try:
            routes = [
                (f"/{PLUGIN_NAME}/words", self._web_get_words, ["GET"], "获取违禁词"),
                (f"/{PLUGIN_NAME}/words/add", self._web_add_words, ["POST"], "添加违禁词"),
                (f"/{PLUGIN_NAME}/words/delete", self._web_delete_words, ["POST"], "删除违禁词"),
                (f"/{PLUGIN_NAME}/words/clear", self._web_clear_words, ["POST"], "清空违禁词"),
                (f"/{PLUGIN_NAME}/violations", self._web_get_violations, ["GET"], "获取违规记录"),
                (f"/{PLUGIN_NAME}/violations/clear", self._web_clear_violations, ["POST"], "清空违规记录"),
                (f"/{PLUGIN_NAME}/admins", self._web_get_admins, ["GET"], "获取管理员列表"),
                (f"/{PLUGIN_NAME}/admins/add", self._web_add_admin, ["POST"], "添加管理员"),
                (f"/{PLUGIN_NAME}/admins/delete", self._web_delete_admin, ["POST"], "删除管理员"),
                (f"/{PLUGIN_NAME}/tokens", self._web_get_token_stats, ["GET"], "获取Token用量统计"),
                (f"/{PLUGIN_NAME}/tokens/config", self._web_save_token_config, ["POST"], "保存Token计费模型配置"),
                (f"/{PLUGIN_NAME}/tokens/history", self._web_get_token_history, ["GET"], "获取Token用量历史（日/月）"),
            ]
            for path, handler, methods, desc in routes:
                self.context.register_web_api(path, self._wrap_web_handler(handler), methods, desc)
            logger.info(f"[文本审核] WebUI 路由已注册: /{PLUGIN_NAME}/words 等12个接口")
        except Exception as e:
            logger.warning(f"[文本审核] WebUI 路由注册失败（不影响插件运行）: {e}")
    
    async def _web_get_words(self):
        """获取所有违禁词"""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    words_data = json.load(f)
            else:
                words_data = {"blocked": [], "warning": []}
            # 直接返回 blocked 和 warning 列表
            result = {
                "blocked": words_data.get("blocked", []),
                "warning": words_data.get("warning", []),
            }
            logger.info(f"[文本审核] WebAPI get_words 返回: blocked={len(result['blocked'])}, warning={len(result['warning'])}")
            from quart import jsonify
            return jsonify(result)
        except Exception as e:
            logger.error(f"[文本审核] 获取违禁词失败: {e}")
            from quart import jsonify
            return jsonify({"blocked": [], "warning": [], "error": str(e)}), 500
    
    async def _web_add_words(self):
        """添加违禁词"""
        from quart import jsonify, request
        try:
            payload = await request.get_json(force=True, silent=True) or {}
            word_type = payload.get("type", "blocked")
            words = payload.get("words", [])
            
            if word_type not in ("blocked", "warning"):
                return jsonify({"ok": False, "message": "类型必须是 blocked 或 warning"}), 400
            if not words:
                return jsonify({"ok": False, "message": "请提供词语"}), 400
            if not isinstance(words, list):
                words = [words]
            
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    words_data = json.load(f)
            else:
                words_data = {"blocked": [], "warning": []}
            if word_type not in words_data:
                words_data[word_type] = []
            
            added = []
            for w in words:
                w = str(w).strip()
                if w and w not in words_data[word_type]:
                    words_data[word_type].append(w)
                    added.append(w)
            
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(words_data, f, ensure_ascii=False, indent=2)
            load_sensitive_words()
            
            return jsonify({"ok": True, "message": f"成功添加 {len(added)} 个", "added": added})
        except Exception as e:
            logger.error(f"[文本审核] 添加违禁词失败: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500
    
    async def _web_delete_words(self):
        """删除违禁词"""
        from quart import jsonify, request
        try:
            payload = await request.get_json(force=True, silent=True) or {}
            word_type = payload.get("type", "blocked")
            words = payload.get("words", [])
            
            if word_type not in ("blocked", "warning"):
                return jsonify({"ok": False, "message": "类型必须是 blocked 或 warning"}), 400
            if not words:
                return jsonify({"ok": False, "message": "请提供词语"}), 400
            if not isinstance(words, list):
                words = [words]
            
            if not os.path.exists(CONFIG_FILE):
                return jsonify({"ok": False, "message": "词库文件不存在"}), 404
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                words_data = json.load(f)
            if word_type not in words_data:
                words_data[word_type] = []
            
            removed = []
            for w in words:
                w = str(w).strip()
                if w in words_data[word_type]:
                    words_data[word_type].remove(w)
                    removed.append(w)
            
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(words_data, f, ensure_ascii=False, indent=2)
            load_sensitive_words()
            
            return jsonify({"ok": True, "message": f"成功删除 {len(removed)} 个", "removed": removed})
        except Exception as e:
            logger.error(f"[文本审核] 删除违禁词失败: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500
    
    async def _web_clear_words(self):
        """清空违禁词"""
        from quart import jsonify, request
        try:
            payload = await request.get_json(force=True, silent=True) or {}
            word_type = payload.get("type", "blocked")
            
            if word_type not in ("blocked", "warning"):
                return jsonify({"ok": False, "message": "类型必须是 blocked 或 warning"}), 400
            
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    words_data = json.load(f)
            else:
                words_data = {"blocked": [], "warning": []}
            
            count = len(words_data.get(word_type, []))
            words_data[word_type] = []
            
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(words_data, f, ensure_ascii=False, indent=2)
            load_sensitive_words()

            return jsonify({"ok": True, "message": f"已清空 {count} 个"})
        except Exception as e:
            logger.error(f"[文本审核] 清空违禁词失败: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    async def _web_get_violations(self):
        """获取违规记录列表"""
        from quart import jsonify
        try:
            # 返回违规详情和用户计数
            result = {
                "details": VIOLATION_DETAILS,
                "counts": VIOLATION_RECORDS
            }
            logger.info(f"[文本审核] WebAPI get_violations 返回: {len(VIOLATION_DETAILS)} 条详情")
            return jsonify(result)
        except Exception as e:
            logger.error(f"[文本审核] 获取违规记录失败: {e}")
            return jsonify({"details": [], "counts": {}, "error": str(e)}), 500

    async def _web_clear_violations(self):
        """清空违规记录"""
        from quart import jsonify, request
        global VIOLATION_RECORDS, VIOLATION_DETAILS
        try:
            payload = await request.get_json(force=True, silent=True) or {}
            user_id = payload.get("user_id")

            if user_id:
                # 清空指定用户的违规记录
                user_id = str(user_id)
                if user_id in VIOLATION_RECORDS:
                    del VIOLATION_RECORDS[user_id]
                VIOLATION_DETAILS = [d for d in VIOLATION_DETAILS if d.get("user_id") != user_id]
                self.save_violation_records()
                return jsonify({"ok": True, "message": f"已清空用户 {user_id} 的违规记录"})
            else:
                # 清空所有违规记录
                count = len(VIOLATION_RECORDS)
                detail_count = len(VIOLATION_DETAILS)
                VIOLATION_RECORDS = {}
                VIOLATION_DETAILS = []
                self.save_violation_records()
                return jsonify({"ok": True, "message": f"已清空 {count} 个用户的记录，{detail_count} 条详情"})
        except Exception as e:
            logger.error(f"[文本审核] 清空违规记录失败: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    async def _web_get_admins(self):
        """获取管理员列表"""
        from quart import jsonify
        try:
            return jsonify({"admins": self.admin_list})
        except Exception as e:
            return jsonify({"admins": [], "error": str(e)}), 500

    async def _web_add_admin(self):
        """添加管理员"""
        from quart import jsonify, request
        try:
            payload = await request.get_json(force=True, silent=True) or {}
            admin_id = str(payload.get("admin_id", "")).strip()
            if not admin_id:
                return jsonify({"ok": False, "message": "请输入管理员QQ号"}), 400
            if admin_id in self.admin_list:
                return jsonify({"ok": False, "message": "该管理员已存在"}), 400

            self.admin_list.append(admin_id)
            # 保存到配置
            self.config['admin_list'] = self.admin_list
            self._save_config()
            logger.info(f"[文本审核] 添加管理员: {admin_id}")
            return jsonify({"ok": True, "message": f"已添加管理员: {admin_id}"})
        except Exception as e:
            logger.error(f"[文本审核] 添加管理员失败: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    async def _web_delete_admin(self):
        """删除管理员"""
        from quart import jsonify, request
        try:
            payload = await request.get_json(force=True, silent=True) or {}
            admin_id = str(payload.get("admin_id", "")).strip()
            if not admin_id:
                return jsonify({"ok": False, "message": "请提供管理员QQ号"}), 400
            if admin_id not in self.admin_list:
                return jsonify({"ok": False, "message": "该管理员不存在"}), 400

            self.admin_list.remove(admin_id)
            # 保存到配置
            self.config['admin_list'] = self.admin_list
            self._save_config()
            logger.info(f"[文本审核] 删除管理员: {admin_id}")
            return jsonify({"ok": True, "message": f"已删除管理员: {admin_id}"})
        except Exception as e:
            logger.error(f"[文本审核] 删除管理员失败: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    async def _web_get_token_stats(self):
        """获取 Token 用量统计"""
        global TOKEN_USAGE, MODEL_PRICES
        from quart import jsonify
        try:
            total = TOKEN_USAGE["total_prompt_tokens"] + TOKEN_USAGE["total_completion_tokens"]
            today = TOKEN_USAGE["today_prompt_tokens"] + TOKEN_USAGE["today_completion_tokens"]
            # 用配置的模型名查找价格，找不到用 custom
            model_key = self.config.get('token_model', 'custom')
            if model_key == 'custom':
                # 自定义模型价格
                price = {
                    "name": self.config.get('token_custom_name', '自定义'),
                    "input": float(self.config.get('token_custom_input', 0.5)),
                    "output": float(self.config.get('token_custom_output', 1.5)),
                    "url": ""
                }
            else:
                price = MODEL_PRICES.get(model_key, MODEL_PRICES['custom'])
            # 估算费用（美元）
            total_cost = (TOKEN_USAGE["total_prompt_tokens"] / 1000000 * price["input"] +
                          TOKEN_USAGE["total_completion_tokens"] / 1000000 * price["output"])
            today_cost = (TOKEN_USAGE["today_prompt_tokens"] / 1000000 * price["input"] +
                          TOKEN_USAGE["today_completion_tokens"] / 1000000 * price["output"])
            return jsonify({
                "total_calls": TOKEN_USAGE["total_calls"],
                "today_calls": TOKEN_USAGE["today_calls"],
                "total_prompt": TOKEN_USAGE["total_prompt_tokens"],
                "total_completion": TOKEN_USAGE["total_completion_tokens"],
                "total_tokens": total,
                "today_prompt": TOKEN_USAGE["today_prompt_tokens"],
                "today_completion": TOKEN_USAGE["today_completion_tokens"],
                "today_tokens": today,
                "total_cost": round(total_cost, 6),
                "today_cost": round(today_cost, 6),
                "model_name": price["name"],
                "model_url": price["url"],
                "custom_name": self.config.get('token_custom_name', ''),
                "custom_input": float(self.config.get('token_custom_input', 0.5)),
                "custom_output": float(self.config.get('token_custom_output', 1.5)),
                "_model_key": model_key,
                "price_info": {"input": price["input"], "output": price["output"]},
                "models": {k: {"name": v["name"], "url": v["url"], "input": v["input"], "output": v["output"]}
                           for k, v in MODEL_PRICES.items()},
                "total_messages": TOKEN_USAGE["total_messages"],
                "total_chars": TOKEN_USAGE["total_chars"],
                "daily_messages": TOKEN_USAGE["daily_messages"],
                "daily_chars": TOKEN_USAGE["daily_chars"],
                "monthly_messages": TOKEN_USAGE["monthly_messages"],
                "monthly_chars": TOKEN_USAGE["monthly_chars"],
            })
        except Exception as e:
            logger.error(f"[文本审核] 获取 Token 统计失败: {e}")
            return jsonify({"error": str(e)}), 500

    async def _web_save_token_config(self):
        """保存 Token 计费模型配置"""
        from quart import jsonify, request
        try:
            data = await request.get_json()
            if not data:
                return jsonify({"ok": False, "message": "无效的请求数据"}), 400
            
            model_key = data.get('model_key', 'custom')
            self.config['token_model'] = model_key
            
            # 保存自定义模型信息
            if model_key == 'custom':
                self.config['token_custom_name'] = data.get('custom_name', '自定义')
                self.config['token_custom_input'] = float(data.get('custom_input', 0.5))
                self.config['token_custom_output'] = float(data.get('custom_output', 1.5))
            
            logger.info(f"[文本审核] Token 计费模型已更新: {model_key}")
            return jsonify({"ok": True, "message": "已保存"})
        except Exception as e:
            logger.error(f"[文本审核] 保存 Token 配置失败: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    async def _web_get_token_history(self):
        """获取 Token 用量历史（每日/每月快照）"""
        global TOKEN_USAGE
        from quart import jsonify
        try:
            daily = dict(TOKEN_USAGE.get("daily_history", {}))
            monthly = dict(TOKEN_USAGE.get("monthly_history", {}))
            return jsonify({
                "daily": daily,
                "monthly": monthly,
            })
        except Exception as e:
            logger.error(f"[文本审核] 获取 Token 历史失败: {e}")
            return jsonify({"daily": {}, "monthly": {}, "error": str(e)}), 500

    def _save_config(self):
        """保存配置到文件"""
        try:
            # AstrBot 配置文件路径
            plugin_name = getattr(self, 'name', 'astrbot_plugin_text_moderation')
            config_path = Path(get_astrbot_data_path()) / "config" / f"{plugin_name}_config.json"
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            logger.info(f"[文本审核] 配置已保存到: {config_path}")
        except Exception as e:
            logger.error(f"[文本审核] 保存配置失败: {e}")

    def get_words_file(self):
        """获取违禁词文件路径"""
        return CONFIG_FILE
    
    def reload_words(self):
        """重新加载违禁词到内存"""
        load_sensitive_words()
        logger.info(f"[文本审核] 违禁词库已重新加载")
    
    def update_config(self, config: dict):
        """配置更新时调用"""
        self.config = config
        self.load_config()
        logger.info("[文本审核] 配置已更新")
    
    def get_user_id(self, event: AstrMessageEvent) -> str:
        """获取用户ID"""
        try:
            if hasattr(event, 'get_sender_id'):
                return str(event.get_sender_id())
            elif hasattr(event, 'sender') and hasattr(event.sender, 'user_id'):
                return str(event.sender.user_id)
            elif hasattr(event, 'user_id'):
                return str(event.user_id)
        except Exception as e:
            logger.debug(f"[文本审核] 获取用户ID失败: {e}")
        return None
    
    def track_violation(self, user_id: str, level: str):
        """
        记录违规行为
        :param user_id: 用户ID
        :param level: 违规级别 ('warning' 或 'block')
        """
        if user_id not in VIOLATION_RECORDS:
            VIOLATION_RECORDS[user_id] = {"warnings": [], "blocks": []}

        current_time = time.time()
        VIOLATION_RECORDS[user_id][level + 's'].append(current_time)

        logger.info(f"[文本审核] 记录违规 - 用户: {user_id}, 级别: {level}")
        # 持久化保存
        self.save_violation_records()

    def record_violation_detail(self, user_id: str, group_id: str, content: str, reason: str, level: str, source: str = 'ai'):
        """
        记录违规详情（用于WebUI展示）
        """
        global VIOLATION_DETAILS
        detail = {
            "user_id": str(user_id),
            "group_id": str(group_id) if group_id else "",
            "content": content[:200],  # 限制长度
            "reason": reason[:200],
            "level": level,
            "timestamp": time.time(),
            "source": source
        }
        VIOLATION_DETAILS.append(detail)
        # 最多保留500条记录
        if len(VIOLATION_DETAILS) > 500:
            VIOLATION_DETAILS = VIOLATION_DETAILS[-500:]
        self.save_violation_records()

    def save_violation_records(self):
        """保存违规记录到文件"""
        global VIOLATION_RECORDS, VIOLATION_DETAILS
        if not VIOLATION_RECORDS_FILE:
            return
        try:
            data = {
                "records": VIOLATION_RECORDS,
                "details": VIOLATION_DETAILS
            }
            with open(VIOLATION_RECORDS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[文本审核] 保存违规记录失败: {e}")

    def load_violation_records(self):
        """从文件加载违规记录"""
        global VIOLATION_RECORDS, VIOLATION_DETAILS
        if not VIOLATION_RECORDS_FILE or not os.path.exists(VIOLATION_RECORDS_FILE):
            return
        try:
            with open(VIOLATION_RECORDS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            VIOLATION_RECORDS = data.get("records", {})
            VIOLATION_DETAILS = data.get("details", [])
            logger.info(f"[文本审核] 违规记录加载完成: {len(VIOLATION_RECORDS)} 个用户, {len(VIOLATION_DETAILS)} 条详情")
        except Exception as e:
            logger.error(f"[文本审核] 加载违规记录失败: {e}")

    # ====== Token 用量追踪 ======

    def _load_token_usage(self):
        """从文件加载 token 用量"""
        global TOKEN_USAGE
        if not TOKEN_USAGE_FILE or not os.path.exists(TOKEN_USAGE_FILE):
            return
        try:
            with open(TOKEN_USAGE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            TOKEN_USAGE.update(data)
            # 确保历史记录字段存在
            if "daily_history" not in TOKEN_USAGE:
                TOKEN_USAGE["daily_history"] = {}
            if "monthly_history" not in TOKEN_USAGE:
                TOKEN_USAGE["monthly_history"] = {}
            # 每日自动重置
            today = time.strftime("%Y-%m-%d")
            if TOKEN_USAGE.get("last_reset_date") != today:
                # 保存上一天的历史
                last_date = TOKEN_USAGE.get("last_reset_date")
                if last_date:
                    TOKEN_USAGE["daily_history"][last_date] = {
                        "calls": TOKEN_USAGE.get("today_calls", 0),
                        "prompt": TOKEN_USAGE.get("today_prompt_tokens", 0),
                        "completion": TOKEN_USAGE.get("today_completion_tokens", 0),
                        "messages": TOKEN_USAGE.get("daily_messages", 0),
                        "chars": TOKEN_USAGE.get("daily_chars", 0),
                    }
                    # 只保留最近 60 天
                    sorted_days = sorted(TOKEN_USAGE["daily_history"].keys())
                    while len(sorted_days) > 60:
                        del TOKEN_USAGE["daily_history"][sorted_days.pop(0)]
                TOKEN_USAGE["today_prompt_tokens"] = 0
                TOKEN_USAGE["today_completion_tokens"] = 0
                TOKEN_USAGE["today_calls"] = 0
                TOKEN_USAGE["daily_messages"] = 0
                TOKEN_USAGE["daily_chars"] = 0
                TOKEN_USAGE["last_reset_date"] = today
                self._save_token_usage()
            # 每月自动重置
            this_month = time.strftime("%Y-%m")
            if TOKEN_USAGE.get("monthly_date") != this_month:
                # 保存上一个月的历史
                last_month = TOKEN_USAGE.get("monthly_date")
                if last_month:
                    TOKEN_USAGE["monthly_history"][last_month] = {
                        "calls": TOKEN_USAGE.get("total_calls", 0) - TOKEN_USAGE.get("_prev_month_calls", 0),
                        "messages": TOKEN_USAGE.get("monthly_messages", 0),
                        "chars": TOKEN_USAGE.get("monthly_chars", 0),
                    }
                    TOKEN_USAGE["_prev_month_calls"] = TOKEN_USAGE.get("total_calls", 0)
                    # 只保留最近 24 个月
                    sorted_months = sorted(TOKEN_USAGE["monthly_history"].keys())
                    while len(sorted_months) > 24:
                        del TOKEN_USAGE["monthly_history"][sorted_months.pop(0)]
                TOKEN_USAGE["monthly_messages"] = 0
                TOKEN_USAGE["monthly_chars"] = 0
                TOKEN_USAGE["monthly_date"] = this_month
                self._save_token_usage()
            logger.info(f"[文本审核] Token 用量加载完成: 总计 {TOKEN_USAGE['total_calls']} 次调用, {TOKEN_USAGE['total_messages']} 条消息")
        except Exception as e:
            logger.error(f"[文本审核] 加载 Token 用量失败: {e}")

    def _save_token_usage(self):
        """保存 token 用量到文件"""
        global TOKEN_USAGE
        if not TOKEN_USAGE_FILE:
            return
        try:
            with open(TOKEN_USAGE_FILE, 'w', encoding='utf-8') as f:
                json.dump(TOKEN_USAGE, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[文本审核] 保存 Token 用量失败: {e}")

    def _save_daily_snapshot(self):
        """保存今日快照到 daily_history（重置前调用）"""
        global TOKEN_USAGE
        last_date = TOKEN_USAGE.get("last_reset_date", "")
        if not last_date:
            return
        TOKEN_USAGE["daily_history"][last_date] = {
            "calls": TOKEN_USAGE.get("today_calls", 0),
            "prompt": TOKEN_USAGE.get("today_prompt_tokens", 0),
            "completion": TOKEN_USAGE.get("today_completion_tokens", 0),
            "messages": TOKEN_USAGE.get("daily_messages", 0),
            "chars": TOKEN_USAGE.get("daily_chars", 0),
        }
        # 只保留最近 60 天
        sorted_days = sorted(TOKEN_USAGE["daily_history"].keys())
        while len(sorted_days) > 60:
            del TOKEN_USAGE["daily_history"][sorted_days.pop(0)]
        self._save_token_usage()

    def _save_monthly_snapshot(self):
        """保存本月快照到 monthly_history（重置前调用）"""
        global TOKEN_USAGE
        last_month = TOKEN_USAGE.get("monthly_date", "")
        if not last_month:
            return
        calls_for_month = TOKEN_USAGE.get("total_calls", 0) - TOKEN_USAGE.get("_prev_month_calls", 0)
        TOKEN_USAGE["monthly_history"][last_month] = {
            "calls": calls_for_month,
            "messages": TOKEN_USAGE.get("monthly_messages", 0),
            "chars": TOKEN_USAGE.get("monthly_chars", 0),
        }
        TOKEN_USAGE["_prev_month_calls"] = TOKEN_USAGE.get("total_calls", 0)
        # 只保留最近 24 个月
        sorted_months = sorted(TOKEN_USAGE["monthly_history"].keys())
        while len(sorted_months) > 24:
            del TOKEN_USAGE["monthly_history"][sorted_months.pop(0)]
        self._save_token_usage()

    def _record_token_usage(self, prompt_chars: int, completion_chars: int):
        """记录一次 AI 调用的 token 用量（估算）"""
        global TOKEN_USAGE
        today = time.strftime("%Y-%m-%d")
        # 检查日期是否变更，自动重置今日计数
        if TOKEN_USAGE.get("last_reset_date") != today:
            self._save_daily_snapshot()
            TOKEN_USAGE["today_prompt_tokens"] = 0
            TOKEN_USAGE["today_completion_tokens"] = 0
            TOKEN_USAGE["today_calls"] = 0
            TOKEN_USAGE["last_reset_date"] = today
        
        # 粗略估算：中文约 1.5 字符/token，英文约 4 字符/token
        # 取较保守的估算：按 2 字符/token 估算
        prompt_tokens = max(1, int(prompt_chars / 2))
        completion_tokens = max(1, int(completion_chars / 2))
        
        TOKEN_USAGE["total_calls"] += 1
        TOKEN_USAGE["total_prompt_tokens"] += prompt_tokens
        TOKEN_USAGE["total_completion_tokens"] += completion_tokens
        TOKEN_USAGE["today_calls"] += 1
        TOKEN_USAGE["today_prompt_tokens"] += prompt_tokens
        TOKEN_USAGE["today_completion_tokens"] += completion_tokens
        
        # 每调用 10 次存一次，减少 IO
        if TOKEN_USAGE["total_calls"] % 10 == 0:
            self._save_token_usage()

    def _record_message_stats(self, text_len: int):
        """记录一条消息的审核统计"""
        global TOKEN_USAGE
        today = time.strftime("%Y-%m-%d")
        this_month = time.strftime("%Y-%m")
        # 日期变更重置
        if TOKEN_USAGE.get("last_reset_date") != today:
            self._save_daily_snapshot()
            TOKEN_USAGE["today_prompt_tokens"] = 0
            TOKEN_USAGE["today_completion_tokens"] = 0
            TOKEN_USAGE["today_calls"] = 0
            TOKEN_USAGE["daily_messages"] = 0
            TOKEN_USAGE["daily_chars"] = 0
            TOKEN_USAGE["last_reset_date"] = today
        if TOKEN_USAGE.get("monthly_date") != this_month:
            self._save_monthly_snapshot()
            TOKEN_USAGE["monthly_messages"] = 0
            TOKEN_USAGE["monthly_chars"] = 0
            TOKEN_USAGE["monthly_date"] = this_month
        
        TOKEN_USAGE["total_messages"] += 1
        TOKEN_USAGE["total_chars"] += text_len
        TOKEN_USAGE["daily_messages"] += 1
        TOKEN_USAGE["daily_chars"] += text_len
        TOKEN_USAGE["monthly_messages"] += 1
        TOKEN_USAGE["monthly_chars"] += text_len
        
        # 每 20 条存一次
        if TOKEN_USAGE["total_messages"] % 20 == 0:
            self._save_token_usage()
    
    def is_admin(self, user_id: str) -> bool:
        """检查用户是否是管理员"""
        return str(user_id) in self.admin_list
    
    def get_violation_count(self, user_id: str, level: str) -> int:
        """
        获取用户在时间窗口内的违规次数
        :param user_id: 用户ID
        :param level: 违规级别 ('warning' 或 'block')
        :return: 违规次数
        """
        if user_id not in VIOLATION_RECORDS:
            return 0
        
        current_time = time.time()
        time_window_seconds = self.punishment_time_window * 60
        
        # 过滤出时间窗口内的违规记录
        records = VIOLATION_RECORDS[user_id].get(level + 's', [])
        valid_records = [r for r in records if current_time - r <= time_window_seconds]
        
        # 更新记录，移除过期的
        VIOLATION_RECORDS[user_id][level + 's'] = valid_records
        
        return len(valid_records)
    
    def get_punishment_desc(self, level: str) -> str:
        """
        获取违规级别的惩罚力度描述
        :param level: 违规级别 ('warning' 或 'block')
        :return: 惩罚力度描述字符串
        """
        if level == 'warning':
            # 轻度违规：仅警告，不计入惩罚次数
            return "仅警告，不计入惩罚次数"
        else:
            # 严重违规：计入惩罚次数（+1 包含本次即将记录的违规）
            current_count = self.get_violation_count(self._current_user_id, 'block') if hasattr(self, '_current_user_id') else 0
            count = current_count + 1  # 加上本次违规
            threshold = self.block_punishment_count
            action = self.block_punishment_action
            duration = self.block_punishment_duration
            
            # 动作描述
            if action == 'mute':
                action_desc = f"禁言 {duration} 分钟"
            elif action == 'kick':
                action_desc = "踢出群聊"
            elif action == 'none':
                action_desc = "仅警告"
            else:
                action_desc = action
            
            return f"累计 {count}/{threshold} 次后将{action_desc}"
    
    async def apply_punishment(self, event: AstrMessageEvent, action: str, duration: int = 0):
        """
        应用惩罚措施（针对 AstrBot + NapCat/OneBot v11 环境）
        使用 platform_manager.get_insts() + platform.get_client() 的正确方式获取客户端
        :param event: 消息事件
        :param action: 惩罚动作 ('mute', 'kick')
        :param duration: 禁言时长（分钟），仅对mute生效
        """
        user_id = self.get_user_id(event)
        group_id = None
        
        # 获取群ID的多种方式
        try:
            if hasattr(event, 'get_group_id'):
                group_id = event.get_group_id()
            elif hasattr(event, 'message_obj') and hasattr(event.message_obj, 'group_id'):
                group_id = event.message_obj.group_id
            elif hasattr(event, 'message') and hasattr(event.message, 'group_id'):
                group_id = event.message.group_id
        except Exception as e:
            logger.debug(f"[文本审核] 获取群ID失败: {e}")
        
        if not user_id or not group_id:
            logger.warning(f"[文本审核] 无法获取用户或群信息，无法执行惩罚")
            return
        
        # 确保ID是整数类型（OneBot API要求整数）
        try:
            user_id_int = int(user_id)
            group_id_int = int(group_id)
        except (ValueError, TypeError):
            logger.warning(f"[文本审核] 用户ID或群ID不是有效整数: user_id={user_id}, group_id={group_id}")
            yield event.plain_result("❌ 执行惩罚失败：ID格式不正确")
            return
        
        logger.info(f"[文本审核] 准备执行惩罚 - 动作: {action}, 用户: {user_id_int}, 群: {group_id_int}, 时长: {duration}分钟")
        
        success = False
        error_msg = ""
        
        # 使用正确的平台管理器方式获取客户端（参考 astrbot_plugin_group_aip_review）
        client = None
        try:
            if hasattr(self.context, 'platform_manager'):
                platforms = self.context.platform_manager.get_insts()
                logger.info(f"[文本审核] 获取到 {len(platforms)} 个平台实例")
                
                # 遍历所有平台，找到支持目标操作的客户端
                target_method = 'set_group_ban' if action == 'mute' else 'set_group_kick'
                for platform in platforms:
                    try:
                        platform_client = platform.get_client()
                        if platform_client and hasattr(platform_client, target_method):
                            client = platform_client
                            logger.info(f"[文本审核] 找到支持 {target_method} 的客户端: {type(client).__name__}")
                            break
                    except Exception as e:
                        logger.debug(f"[文本审核] 获取平台客户端失败: {e}")
                        continue
            else:
                logger.warning("[文本审核] context 没有 platform_manager 属性")
        except Exception as e:
            logger.error(f"[文本审核] 获取平台实例失败: {e}")
        
        if not client:
            yield event.plain_result(f"⚠️ 惩罚操作执行失败。\n原因：未找到支持该操作的平台客户端")
            logger.error(f"[文本审核] 未找到支持 {target_method} 的客户端")
            return
        
        if action == 'mute':
            # 禁言操作 - 将分钟转换为秒
            duration_seconds = duration * 60
            
            # 方式1: 使用 client.set_group_ban（直接方法）
            if hasattr(client, 'set_group_ban'):
                try:
                    await client.set_group_ban(
                        group_id=group_id_int,
                        user_id=user_id_int,
                        duration=duration_seconds
                    )
                    logger.info(f"[文本审核] 禁言成功 (set_group_ban)")
                    success = True
                except Exception as api_error:
                    error_msg = str(api_error)
                    logger.warning(f"[文本审核] set_group_ban禁言失败: {api_error}")
            
            # 方式2: 使用 client.call_action
            if not success and hasattr(client, 'call_action'):
                try:
                    await client.call_action(
                        'set_group_ban',
                        group_id=group_id_int,
                        user_id=user_id_int,
                        duration=duration_seconds
                    )
                    logger.info(f"[文本审核] 禁言成功 (call_action)")
                    success = True
                except Exception as api_error:
                    error_msg = str(api_error)
                    logger.warning(f"[文本审核] call_action禁言失败: {api_error}")
            
            # 方式3: 使用 client.call_api
            if not success and hasattr(client, 'call_api'):
                try:
                    await client.call_api(
                        'set_group_ban',
                        group_id=group_id_int,
                        user_id=user_id_int,
                        duration=duration_seconds
                    )
                    logger.info(f"[文本审核] 禁言成功 (call_api)")
                    success = True
                except Exception as api_error:
                    error_msg = str(api_error)
                    logger.warning(f"[文本审核] call_api禁言失败: {api_error}")
            
            if success:
                yield event.plain_result(f"🔇 用户 {user_id_int} 因多次违规已被禁言 {duration} 分钟")
            else:
                yield event.plain_result(f"⚠️ 禁言操作执行失败。请确保机器人具有管理员权限。\n错误信息: {error_msg[:100]}")
                logger.error(f"[文本审核] 所有方式都无法执行禁言操作")
        
        elif action == 'kick':
            # 踢出操作 - 不需要时长参数
            
            # 方式1: 使用 client.set_group_kick（直接方法）
            if hasattr(client, 'set_group_kick'):
                try:
                    await client.set_group_kick(
                        group_id=group_id_int,
                        user_id=user_id_int,
                        reject_add_request=True
                    )
                    logger.info(f"[文本审核] 踢出成功 (set_group_kick)")
                    success = True
                except Exception as api_error:
                    error_msg = str(api_error)
                    logger.warning(f"[文本审核] set_group_kick踢出失败: {api_error}")
            
            # 方式2: 使用 client.call_action
            if not success and hasattr(client, 'call_action'):
                try:
                    await client.call_action(
                        'set_group_kick',
                        group_id=group_id_int,
                        user_id=user_id_int,
                        reject_add_request=True
                    )
                    logger.info(f"[文本审核] 踢出成功 (call_action)")
                    success = True
                except Exception as api_error:
                    error_msg = str(api_error)
                    logger.warning(f"[文本审核] call_action踢出失败: {api_error}")
            
            # 方式3: 使用 client.call_api
            if not success and hasattr(client, 'call_api'):
                try:
                    await client.call_api(
                        'set_group_kick',
                        group_id=group_id_int,
                        user_id=user_id_int,
                        reject_add_request=True
                    )
                    logger.info(f"[文本审核] 踢出成功 (call_api)")
                    success = True
                except Exception as api_error:
                    error_msg = str(api_error)
                    logger.warning(f"[文本审核] call_api踢出失败: {api_error}")
            
            if success:
                yield event.plain_result(f"👢 用户 {user_id_int} 因严重违规已被踢出群")
            else:
                yield event.plain_result(f"⚠️ 踢出操作执行失败。请确保机器人具有管理员权限。\n错误信息: {error_msg[:100]}")
                logger.error(f"[文本审核] 所有方式都无法执行踢出操作")
        else:
            logger.info(f"[文本审核] 无惩罚动作")
    
    async def recall_message(self, event: AstrMessageEvent):
        """
        撤回违规消息（针对 AstrBot + NapCat/OneBot v11 环境）
        """
        try:
            # 获取消息ID
            message_id = None
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'message_id'):
                message_id = event.message_obj.message_id
            elif hasattr(event, 'message_id'):
                message_id = event.message_id
            
            if not message_id:
                logger.warning("[文本审核] 无法获取消息ID，无法撤回消息")
                return False
            
            # 使用正确的平台管理器方式获取客户端
            client = None
            if hasattr(self.context, 'platform_manager'):
                platforms = self.context.platform_manager.get_insts()
                for platform in platforms:
                    try:
                        platform_client = platform.get_client()
                        if platform_client and hasattr(platform_client, 'delete_msg'):
                            client = platform_client
                            break
                    except Exception as e:
                        continue
            
            if not client:
                logger.warning("[文本审核] 未找到支持撤回消息的客户端")
                return False
            
            # 尝试多种方式撤回消息
            success = False
            if hasattr(client, 'delete_msg'):
                try:
                    await client.delete_msg(message_id=message_id)
                    success = True
                except Exception as e:
                    logger.debug(f"[文本审核] delete_msg撤回失败: {e}")
            
            if not success and hasattr(client, 'call_action'):
                try:
                    await client.call_action('delete_msg', message_id=message_id)
                    success = True
                except Exception as e:
                    logger.debug(f"[文本审核] call_action撤回失败: {e}")
            
            if not success and hasattr(client, 'call_api'):
                try:
                    await client.call_api('delete_msg', message_id=message_id)
                    success = True
                except Exception as e:
                    logger.debug(f"[文本审核] call_api撤回失败: {e}")
            
            if success:
                logger.info(f"[文本审核] 消息撤回成功 - message_id: {message_id}")
            else:
                logger.warning(f"[文本审核] 消息撤回失败 - message_id: {message_id}")
            
            return success
        except Exception as e:
            logger.error(f"[文本审核] 撤回消息异常: {e}")
            return False

    async def notify_admins(self, user_id: str, group_id: str, content: str, reason: str, level: str):
        """
        私信通知管理员违规信息
        """
        if not self.enable_admin_notify or not self.admin_list:
            return

        # 获取平台客户端
        client = None
        try:
            if hasattr(self.context, 'platform_manager'):
                platforms = self.context.platform_manager.get_insts()
                for platform in platforms:
                    try:
                        platform_client = platform.get_client()
                        if platform_client and hasattr(platform_client, 'send_private_msg'):
                            client = platform_client
                            break
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"[文本审核] 获取通知客户端失败: {e}")

        if not client:
            logger.warning("[文本审核] 未找到支持私信的客户端，无法通知管理员")
            return

        # 构建通知消息
        level_emoji = "🔴" if level == 'block' else "🟡"
        level_text = "严重违规" if level == 'block' else "轻度违规"
        msg = (
            f"{level_emoji} 文本审核违规通知\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 用户ID: {user_id}\n"
            f"👥 群号: {group_id}\n"
            f"📋 违规等级: {level_text}\n"
            f"📝 违规内容: {content[:100]}\n"
            f"⚠️ 违规原因: {reason[:100]}\n"
            f"⏰ 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        # 发送私信给每个管理员
        for admin_id in self.admin_list:
            try:
                admin_id_int = int(admin_id)
                success = False
                # 方式1: 直接调用
                if hasattr(client, 'send_private_msg'):
                    try:
                        await client.send_private_msg(user_id=admin_id_int, message=msg)
                        success = True
                    except Exception as e:
                        logger.debug(f"[文本审核] send_private_msg失败: {e}")
                # 方式2: call_action
                if not success and hasattr(client, 'call_action'):
                    try:
                        await client.call_action('send_private_msg', user_id=admin_id_int, message=msg)
                        success = True
                    except Exception as e:
                        logger.debug(f"[文本审核] call_action私信失败: {e}")
                # 方式3: call_api
                if not success and hasattr(client, 'call_api'):
                    try:
                        await client.call_api('send_private_msg', user_id=admin_id_int, message=msg)
                        success = True
                    except Exception as e:
                        logger.debug(f"[文本审核] call_api私信失败: {e}")

                if success:
                    logger.info(f"[文本审核] 已私信通知管理员 {admin_id}")
                else:
                    logger.warning(f"[文本审核] 通知管理员 {admin_id} 失败")
            except Exception as e:
                logger.warning(f"[文本审核] 通知管理员 {admin_id} 异常: {e}")

    async def check_punishment(self, event: AstrMessageEvent, level: str):
        """
        检查是否需要执行惩罚（分级管控）
        - 轻度违规(warning)：仅警告，不计入惩罚次数
        - 严重违规(block)：计入惩罚次数，达到阈值后禁言或踢出
        :param event: 消息事件
        :param level: 违规级别 ('warning' 或 'block')
        """
        # 轻度违规：仅警告，不计入惩罚次数，不触发禁言/踢人
        if level == 'warning':
            logger.info(f"[文本审核] 轻度违规，仅警告，不计入惩罚次数")
            return
        
        # 以下是严重违规(block)的处理逻辑
        
        # 检查总开关
        if not self.enable_punishment:
            return
            
        user_id = self.get_user_id(event)
        if not user_id:
            return
        
        # 检查封禁惩罚开关
        if not self.enable_block_punishment:
            return
        
        # 记录严重违规
        self.track_violation(user_id, level)
        
        # 获取违规次数和对应的惩罚动作
        count = self.get_violation_count(user_id, 'block')
        threshold = self.block_punishment_count
        action = self.block_punishment_action
        duration = self.block_punishment_duration if action == 'mute' else 0
        
        logger.info(f"[文本审核] 用户 {user_id} 严重违规 {count}/{threshold}次")
        
        # 如果达到阈值，执行惩罚
        if count >= threshold and action != 'none':
            # 输出惩罚力度信息
            if action == 'mute':
                punishment_desc = f"禁言 {duration} 分钟"
            elif action == 'kick':
                punishment_desc = "踢出群聊"
            else:
                punishment_desc = action
            
            logger.info(f"[文本审核] 触发惩罚 - 用户: {user_id}, 动作: {punishment_desc}")
            async for result in self.apply_punishment(event, action, duration):
                yield result
    
    @filter.command("查看违规记录", alias={'check_violations', 'violation_log'})
    async def handle_check_violations(self, event: AstrMessageEvent):
        """
        查看指定用户的违规记录
        使用方法: /查看违规记录 [用户ID]
        """
        message_str = event.message_str
        parts = message_str.split()
        
        if len(parts) > 1:
            user_id = parts[1]
        else:
            # 默认查看当前用户
            user_id = self.get_user_id(event)
        
        if not user_id:
            yield event.plain_result("❌ 无法获取用户ID")
            return
        
        if user_id not in VIOLATION_RECORDS:
            yield event.plain_result(f"✅ 用户 {user_id} 暂无违规记录")
            return
        
        warnings = VIOLATION_RECORDS[user_id].get('warnings', [])
        blocks = VIOLATION_RECORDS[user_id].get('blocks', [])
        
        response = f"📋 用户 {user_id} 违规记录\n\n"
        response += f"🟡 警告 ({len(warnings)}次):\n"
        for w in warnings:
            response += f"  - {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(w))}\n"
        response += f"\n🔴 封禁 ({len(blocks)}次):\n"
        for b in blocks:
            response += f"  - {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(b))}\n"
        
        yield event.plain_result(response)
    
    @filter.command("清空违规记录", alias={'clear_violations'})
    async def handle_clear_violations(self, event: AstrMessageEvent):
        """
        清空指定用户的违规记录（需要管理员权限）
        使用方法: /清空违规记录 [用户ID]
        """
        # 检查管理员权限
        current_user = self.get_user_id(event)
        if not current_user or not self.is_admin(current_user):
            yield event.plain_result("⛔ 此操作需要管理员权限。请联系管理员执行。")
            return

        message_str = event.message_str
        parts = message_str.split()

        if len(parts) > 1:
            user_id = parts[1]
        else:
            user_id = self.get_user_id(event)

        if not user_id:
            yield event.plain_result("❌ 无法获取用户ID")
            return

        if user_id in VIOLATION_RECORDS:
            del VIOLATION_RECORDS[user_id]
            self.save_violation_records()
            yield event.plain_result(f"✅ 用户 {user_id} 的违规记录已清空")
        else:
            yield event.plain_result(f"❌ 用户 {user_id} 没有违规记录")
    
    @filter.command("查看黑名单", alias={'show_blacklist', 'blacklist'})
    async def handle_show_blacklist(self, event: AstrMessageEvent):
        """
        查看黑名单
        """
        if not BLACKLIST:
            yield event.plain_result("✅ 黑名单为空")
            return
        
        response = "⛔ 黑名单列表:\n"
        for user_id in BLACKLIST:
            response += f"  - {user_id}\n"
        
        yield event.plain_result(response)
    
    @filter.command("移出黑名单", alias={'remove_blacklist', 'unban'})
    async def handle_remove_blacklist(self, event: AstrMessageEvent):
        """
        将用户移出黑名单（需要管理员权限）
        使用方法: /移出黑名单 [用户ID]
        """
        # 检查管理员权限
        current_user = self.get_user_id(event)
        if not current_user or not self.is_admin(current_user):
            yield event.plain_result("⛔ 此操作需要管理员权限。请联系管理员执行。")
            return

        message_str = event.message_str
        parts = message_str.split()
        
        if len(parts) < 2:
            yield event.plain_result("请输入用户ID\n例如: /移出黑名单 123456789")
            return
        
        user_id = parts[1]
        
        if user_id in BLACKLIST:
            BLACKLIST.remove(user_id)
            yield event.plain_result(f"✅ 用户 {user_id} 已移出黑名单")
        else:
            yield event.plain_result(f"❌ 用户 {user_id} 不在黑名单中")
    
    async def ai_moderation(self, text: str, event: AstrMessageEvent = None):
        """
        使用AI进行文本违规识别（极简输出格式，省token）
        返回: {"is_violation": True/False, "level": "BLOCK"/"WARNING"/"SAFE", "keywords": [...], "context_dependent": True/False}
        """
        try:
            # 优先使用配置中指定的AI提供商
            if self.ai_provider:
                provider_id = self.ai_provider
                logger.info(f"[文本审核] 使用配置中的AI提供商: {provider_id}")
            elif event:
                # 否则获取当前会话的AI提供商ID
                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            else:
                # 如果没有event，尝试获取默认配置
                provider_id = None
            
            # 如果无法获取provider_id，返回安全结果
            if not provider_id:
                logger.warning("[文本审核] 无法获取AI提供商ID，跳过AI审核")
                return {"is_violation": False, "level": "SAFE", "keywords": [], "context_dependent": False}
            
            # 构建提示词
            # 获取白名单词，在 prompt 中告诉 AI 这些词的语境规则
            whitelist = self.word_whitelist if hasattr(self, 'word_whitelist') else []
            whitelist_str = "、".join(whitelist) if whitelist else "无"
            
            prompt = f"""审核以下群聊消息，仅返回一个标记，不要其他任何文字。

B=严重违规(色情/涉政/暴力/性暗示未成年/人身攻击)
W=轻度违规(辱骂/阴阳怪气对人/低俗)
S=安全

关键规则：
- 性暗示+未成年/萝莉 → B
- 阴阳怪气对人=W,对事=S
- {whitelist_str}=二次元正常用词
- 单独"大调查"无萝莉=W,"大调查"+"萝莉"=B
- 单独"香草"无萝莉=S,"香草"+"萝莉"=B

输出格式(严格，禁止其他文字)：
S | B:理由 | B:理由:kw1,kw2 | W:理由 | W:理由:kw1,kw2
理由用2-4字，如：涉黄/涉政/暴力/性暗示未成年/人身攻击/辱骂/阴阳怪气
> 带关键词=绝对违规进词库。不带=组合违规不进词库。

待审核文本：
{text}"""
            
            # 调用AI
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            
            # 解析结果（极简格式：S | B | B:kw1,kw2 | W | W:kw1,kw2）
            result_text = llm_resp.completion_text.strip()
            logger.info(f"[文本审核] AI审核结果: {result_text}")
            
            # 记录 token 用量估算
            self._record_token_usage(len(prompt), len(result_text))
            
            # 统一分隔符
            result_text = result_text.replace('，', ',').strip()
            
            if result_text == 'S':
                return {"is_violation": False, "level": "SAFE", "keywords": [], "context_dependent": False}
            
            if result_text.startswith('B'):
                level = 'BLOCK'
            elif result_text.startswith('W'):
                level = 'WARNING'
            else:
                return {"is_violation": False, "level": "SAFE", "keywords": [], "context_dependent": False}
            
            # 解析理由和关键词：B:理由 | B:理由:kw1,kw2 | W:理由 | W:理由:kw1,kw2
            reason_type = ''
            keywords = []
            context_dependent = True  # 默认语境依赖，不收关键词
            parts = result_text.split(':', 2)
            if len(parts) >= 2:
                reason_type = parts[1].strip()
            if len(parts) >= 3:
                kw_str = parts[2].strip()
                if kw_str:
                    keywords = [kw.strip() for kw in kw_str.split(',') if kw.strip()]
                    context_dependent = False  # 有关键词=绝对违规，可收集
            
            return {"is_violation": True, "level": level, "reason_type": reason_type, "keywords": keywords, "context_dependent": context_dependent}

        except Exception as e:
            logger.error(f"[文本审核] AI审核失败: {e}")
            return {"is_violation": False, "level": "SAFE", "keywords": [], "context_dependent": False}
    
    def auto_collect_keywords(self, keywords: list, level: str, context_dependent: bool = False):
        """
        自动收集AI发现的违禁词到词库
        :param keywords: 违规关键词列表
        :param level: 违规级别 ('BLOCK' 或 'WARNING')
        :param context_dependent: 是否为语境依赖型违规（如"香草萝莉"，单独词不违规）
        """
        if not self.enable_auto_collect or not keywords:
            return

        # 语境依赖型违规不收集关键词：单独词不违规，收集会造成大面积误判
        # 这类违规每次都靠 AI 判断，不进关键词库
        if context_dependent:
            logger.info(f"[文本审核] 语境依赖型违规，不收集关键词（避免单独词误判）: {keywords}")
            return

        # 映射级别到词库类型
        word_type = 'blocked' if level == 'BLOCK' else 'warning'

        # 加载当前词库
        words_file = CONFIG_FILE
        try:
            if os.path.exists(words_file):
                with open(words_file, 'r', encoding='utf-8') as f:
                    words_data = json.load(f)
            else:
                words_data = {"blocked": [], "warning": []}

            # 确保有对应的列表
            if word_type not in words_data:
                words_data[word_type] = []

            # 过滤白名单词，避免误收集
            whitelist = self.word_whitelist if hasattr(self, 'word_whitelist') else []

            def is_whitelisted(word):
                """检查词语是否在白名单中（完全匹配或包含白名单词）"""
                word_lower = word.lower()
                for w in whitelist:
                    w_lower = w.lower()
                    # 完全匹配
                    if word_lower == w_lower:
                        return True
                    # 如果关键词包含白名单词（如"萝莉控"包含"萝莉"），也跳过
                    if w_lower in word_lower:
                        return True
                return False

            # 添加新词
            added = []
            for kw in keywords:
                kw = str(kw).strip()
                if not kw or len(kw) < 2:
                    continue
                # 跳过白名单词（完全匹配或包含白名单词）
                if is_whitelisted(kw):
                    logger.info(f"[文本审核] 自动收集跳过白名单词: {kw}")
                    continue
                # 跳过歧义词（单独使用正常，只在特定组合下违规）
                # 注意：不限制长度，3字歧义词如"大调查"也必须跳过
                if kw in AMBIGUOUS_WORDS:
                    logger.info(f"[文本审核] 自动收集跳过歧义词: {kw}（单独使用正常，不单独收集，靠AI判断）")
                    continue
                # 跳过过短的词（2字以下的谐音词容易误判）
                if len(kw) < 3 and word_type == 'blocked':
                    # 只允许已知的明确违规词
                    known_explicit = {'傻逼', '约炮', '大屌', '想草', '想操', '去死', '脑残', '废物',
                                     '弓虽女干', '强*奸', '强.奸', '强_奸', 'rape'}
                    if kw not in known_explicit:
                        logger.info(f"[文本审核] 自动收集跳过短词: {kw}（长度<3，可能误判，不单独收集）")
                        continue
                # 避免重复
                if kw not in words_data[word_type]:
                    words_data[word_type].append(kw)
                    added.append(kw)

            # 保存词库
            if added:
                with open(words_file, 'w', encoding='utf-8') as f:
                    json.dump(words_data, f, ensure_ascii=False, indent=2)
                # 重新加载词库
                global SENSITIVE_WORDS
                SENSITIVE_WORDS = words_data
                logger.info(f"[文本审核] AI自动收集违禁词 - 级别: {word_type}, 新增: {added}")
        except Exception as e:
            logger.error(f"[文本审核] 自动收集违禁词失败: {e}")
    
    @filter.command("审核", alias={'text_check', 'check_text'})
    async def handle_text_moderation(self, event: AstrMessageEvent):
        """
        指令触发的文本审核（使用AI）
        使用方法: /审核 [文本内容]
        """
        message_str = event.message_str
        # 获取指令后面的参数
        if message_str.startswith('/审核'):
            text = message_str[3:].strip()
        elif message_str.startswith('/text_check'):
            text = message_str[11:].strip()
        elif message_str.startswith('/check_text'):
            text = message_str[11:].strip()
        else:
            text = ""
        
        if not text:
            yield event.plain_result("请输入需要审核的文本内容\n例如: /审核 这是一段需要审核的文本")
            return
        
        # 使用AI进行审核
        yield event.plain_result("🔍 正在使用AI进行文本审核，请稍候...")
        
        ai_result = await self.ai_moderation(text, event)
        
        if ai_result['is_violation']:
            reason = ai_result.get('reason_type', '')
            reason_str = f"类型：{reason}" if reason else ""
            if ai_result['level'] == 'BLOCK':
                response = f"🚫 检测到严重违规内容！\n\n级别：封禁\n{reason_str}"
            else:
                response = f"💡 检测到轻度违规内容！\n\n级别：警告\n{reason_str}"
            yield event.plain_result(response)
        else:
            yield event.plain_result("✅ 文本通过审核")
    
    @filter.command("添加敏感词", alias={'add_sensitive', 'add_word'})
    async def handle_add_word(self, event: AstrMessageEvent):
        """
        添加敏感词
        使用方法: /添加敏感词 [类型] [词语]
        类型: blocked(封禁)/warning(警告)
        """
        message_str = event.message_str
        parts = message_str.split()
        
        if len(parts) < 3:
            yield event.plain_result("请输入完整参数\n例如: /添加敏感词 blocked 垃圾")
            return
        
        word_type = parts[1].lower()
        words = parts[2:]
        
        if word_type not in ['blocked', 'warning']:
            yield event.plain_result("类型错误! 请使用 blocked 或 warning")
            return
        
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            for word in words:
                if word not in config.get(word_type, []):
                    config[word_type].append(word)
            
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            
            load_sensitive_words()
            yield event.plain_result(f"✅ 成功添加 {len(words)} 个{word_type}类型的敏感词")
        except Exception as e:
            yield event.plain_result(f"❌ 添加失败: {e}")
    
    @filter.command("删除敏感词", alias={'del_sensitive', 'del_word'})
    async def handle_del_word(self, event: AstrMessageEvent):
        """
        删除敏感词
        使用方法: /删除敏感词 [类型] [词语]
        类型: blocked(封禁)/warning(警告)
        """
        message_str = event.message_str
        parts = message_str.split()
        
        if len(parts) < 3:
            yield event.plain_result("请输入完整参数\n例如: /删除敏感词 blocked 垃圾")
            return
        
        word_type = parts[1].lower()
        words = parts[2:]
        
        if word_type not in ['blocked', 'warning']:
            yield event.plain_result("类型错误! 请使用 blocked 或 warning")
            return
        
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            removed_count = 0
            for word in words:
                if word in config.get(word_type, []):
                    config[word_type].remove(word)
                    removed_count += 1
            
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            
            load_sensitive_words()
            yield event.plain_result(f"✅ 成功删除 {removed_count} 个敏感词")
        except Exception as e:
            yield event.plain_result(f"❌ 删除失败: {e}")
    
    @filter.command("查看敏感词", alias={'list_sensitive', 'show_words'})
    async def handle_list_words(self, event: AstrMessageEvent):
        """
        查看敏感词列表
        使用方法: /查看敏感词 [类型(可选)]
        """
        message_str = event.message_str
        parts = message_str.split()
        
        if len(parts) > 1:
            word_type = parts[1].lower()
            if word_type not in ['blocked', 'warning']:
                yield event.plain_result("类型错误! 请使用 blocked 或 warning")
                return
            
            words = SENSITIVE_WORDS.get(word_type, [])
            if words:
                yield event.plain_result(f"{word_type} 类型敏感词 ({len(words)}个):\n" + "\n".join(words))
            else:
                yield event.plain_result(f"{word_type} 类型暂无敏感词")
        else:
            blocked = SENSITIVE_WORDS.get('blocked', [])
            warning = SENSITIVE_WORDS.get('warning', [])
            response = f"📋 敏感词列表\n\n"
            response += f"🔴 封禁词 ({len(blocked)}个):\n"
            response += ", ".join(blocked) if blocked else "无\n"
            response += f"\n🟡 警告词 ({len(warning)}个):\n"
            response += ", ".join(warning) if warning else "无"
            yield event.plain_result(response)
    
    @filter.command("重载敏感词", alias={'reload_words'})
    async def handle_reload_words(self, event: AstrMessageEvent):
        """
        重新加载敏感词配置
        """
        load_sensitive_words()
        yield event.plain_result("✅ 敏感词配置已重新加载")
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_moderation(self, event: AstrMessageEvent):
        """
        自动审核所有消息（混合模式：关键词+AI）
        先进行关键词快速检查，再使用AI进行深度审核
        检测到违规内容时拦截消息或发送提醒
        """
        # 检查是否启用自动审核
        if not self.enable_auto_moderation:
            return
        
        # 检查用户是否在黑名单中（量刑管控和黑名单开关都启用时）
        if self.enable_punishment and self.enable_blacklist:
            user_id = self.get_user_id(event)
            if user_id and user_id in BLACKLIST:
                logger.info(f"[文本审核] 用户 {user_id} 已在黑名单中，直接拦截")
                yield event.plain_result("⛔ 您已被拉黑，无法发送消息")
                event.stop_event()
                return
        
        # 获取消息来源的群号（使用官方API）
        group_id = None
        try:
            # 使用 AstrMessageEvent 提供的 get_group_id() 方法
            if hasattr(event, 'get_group_id'):
                group_id = event.get_group_id()
                # 确保是字符串类型
                if group_id is not None:
                    group_id = str(group_id)
            # 兼容旧版本的备用方案
            elif hasattr(event, 'message') and hasattr(event.message, 'group_id'):
                group_id = str(event.message.group_id)
        except Exception as e:
            logger.debug(f"[文本审核] 获取群号失败: {e}")
        
        logger.debug(f"[文本审核] 当前群号: {group_id}, 允许的群号: {self.allowed_groups}")
        
        # 如果配置了允许的群号列表，检查当前群是否在列表中
        if self.allowed_groups:
            # 如果有群号列表但无法获取当前群号，跳过审核（可能是私聊）
            if group_id is None:
                logger.debug(f"[文本审核] 无法获取群号，跳过审核")
                return
            # 检查当前群是否在允许列表中
            if group_id not in self.allowed_groups:
                # 当前群不在允许列表中，跳过审核
                logger.debug(f"[文本审核] 群 {group_id} 不在允许列表中，跳过审核")
                return
        
        message_str = event.message_str
        
        # 忽略指令消息和空消息
        if message_str.startswith('/') or not message_str.strip():
            return
        
        # 忽略过短的消息，避免浪费API调用
        if len(message_str.strip()) < self.min_message_length:
            return
        
        # 记录消息统计
        self._record_message_stats(len(message_str))
        
        # 第一步：关键词快速检查（如果启用）
        if self.enable_keyword_moderation:
            keyword_result = check_sensitive_words(message_str)
            
            if keyword_result['has_sensitive']:
                # 先尝试撤回消息
                if self.enable_recall_message:
                    await self.recall_message(event)
                
                if keyword_result['blocked']:
                    # 关键词检测到封禁词（严重违规）
                    # 临时保存用户ID用于生成惩罚力度描述
                    self._current_user_id = self.get_user_id(event)
                    violation_content = ', '.join(keyword_result['blocked'])
                    punishment_desc = self.get_punishment_desc('block')
                    response = f"🚫 检测到严重违规内容！消息已撤回\n\n"
                    response += f"━━━━━━━━━━━━━━━\n"
                    response += f"📋 违规等级：🔴 严重违规\n"
                    response += f"📝 违规内容：{violation_content}\n"
                    response += f"⚖️ 惩罚力度：{punishment_desc}\n"
                    response += f"━━━━━━━━━━━━━━━"
                    yield event.plain_result(response)
                    # 记录违规详情并通知管理员
                    self.record_violation_detail(self._current_user_id, group_id, message_str, violation_content, 'block', 'keyword')
                    await self.notify_admins(self._current_user_id, group_id or '', message_str, violation_content, 'block')
                    # 根据开关决定是否拦截消息
                    if self.enable_intercept_message:
                        event.stop_event()

                    # 检查是否需要执行惩罚
                    async for result in self.check_punishment(event, 'block'):
                        yield result
                    return
                elif keyword_result['warning']:
                    # 关键词检测到警告词（轻度违规）
                    self._current_user_id = self.get_user_id(event)
                    violation_content = ', '.join(keyword_result['warning'])
                    punishment_desc = self.get_punishment_desc('warning')
                    response = f"💡 检测到轻度违规内容！消息已撤回\n\n"
                    response += f"━━━━━━━━━━━━━━━\n"
                    response += f"📋 违规等级：🟡 轻度违规\n"
                    response += f"📝 违规内容：{violation_content}\n"
                    response += f"⚖️ 惩罚力度：{punishment_desc}\n"
                    response += f"━━━━━━━━━━━━━━━\n"
                    response += f"请文明用语，避免再次违规。"
                    yield event.plain_result(response)
                    # 记录违规详情并通知管理员
                    self.record_violation_detail(self._current_user_id, group_id, message_str, violation_content, 'warning', 'keyword')
                    await self.notify_admins(self._current_user_id, group_id or '', message_str, violation_content, 'warning')

                    # 检查是否需要执行惩罚
                    async for result in self.check_punishment(event, 'warning'):
                        yield result
                    return
        
        # 第二步：关键词检查通过，使用AI进行深度审核（如果启用）
        if not self.enable_ai_moderation:
            return
        
        try:
            ai_result = await self.ai_moderation(message_str, event)
            
            if ai_result['is_violation']:
                # 先尝试撤回消息
                if self.enable_recall_message:
                    await self.recall_message(event)
                
                # 自动收集AI发现的违禁词到词库（语境依赖型违规不收集，避免单独词误判）
                if ai_result.get('keywords'):
                    self.auto_collect_keywords(
                        ai_result['keywords'],
                        ai_result['level'],
                        ai_result.get('context_dependent', False)
                    )
                
                if ai_result['level'] == 'BLOCK':
                    # AI检测到封禁级别违规（严重违规）
                    self._current_user_id = self.get_user_id(event)
                    punishment_desc = self.get_punishment_desc('block')
                    reason_type = ai_result.get('reason_type', '')
                    reason_line = f"📌 违规类型：{reason_type}\n" if reason_type else ""
                    response = f"🚫 检测到严重违规内容！消息已撤回\n\n"
                    response += f"━━━━━━━━━━━━━━━\n"
                    response += f"📋 违规等级：🔴 严重违规\n"
                    response += reason_line
                    response += f"⚖️ 惩罚力度：{punishment_desc}\n"
                    response += f"━━━━━━━━━━━━━━━"
                    yield event.plain_result(response)
                    # 记录违规详情并通知管理员
                    violation_reason = f"AI:严重违规({reason_type})" if reason_type else 'AI:严重违规'
                    self.record_violation_detail(self._current_user_id, group_id, message_str, violation_reason, 'block', 'ai')
                    await self.notify_admins(self._current_user_id, group_id or '', message_str, violation_reason, 'block')
                    # 根据开关决定是否拦截消息
                    if self.enable_intercept_message:
                        event.stop_event()

                    # 检查是否需要执行惩罚
                    async for result in self.check_punishment(event, 'block'):
                        yield result
                elif ai_result['level'] == 'WARNING':
                    # AI检测到警告级别违规（轻度违规）
                    self._current_user_id = self.get_user_id(event)
                    punishment_desc = self.get_punishment_desc('warning')
                    reason_type = ai_result.get('reason_type', '')
                    reason_line = f"📌 违规类型：{reason_type}\n" if reason_type else ""
                    response = f"💡 检测到轻度违规内容！消息已撤回\n\n"
                    response += f"━━━━━━━━━━━━━━━\n"
                    response += f"📋 违规等级：🟡 轻度违规\n"
                    response += reason_line
                    response += f"⚖️ 惩罚力度：{punishment_desc}\n"
                    response += f"━━━━━━━━━━━━━━━\n"
                    response += f"请文明用语，避免再次违规。"
                    yield event.plain_result(response)
                    # 记录违规详情并通知管理员
                    violation_reason = f"AI:轻度违规({reason_type})" if reason_type else 'AI:轻度违规'
                    self.record_violation_detail(self._current_user_id, group_id, message_str, violation_reason, 'warning', 'ai')
                    await self.notify_admins(self._current_user_id, group_id or '', message_str, violation_reason, 'warning')

                    # 检查是否需要执行惩罚
                    async for result in self.check_punishment(event, 'warning'):
                        yield result
        except Exception as e:
            # AI审核失败，记录日志但不影响消息正常处理
            logger.error(f"[文本审核] 自动审核中AI调用失败: {e}")
