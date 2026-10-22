#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浮词 WordFloat —— macOS 桌面悬浮球生词本

运行：python3 wordfloat.py        （需要 pyobjc-framework-Cocoa，启动器会自动安装）
数据：~/WordFloat/   words.json 生词 · config.json 设置 · backups/ 每日备份

· 桌面悬浮球。鼠标移上去展开输入框，剪贴板里的英文单词自动填好，回车即记录。
· 中文释义由大模型生成。默认智谱 GLM-4.7-Flash（免费）；也可换 DeepSeek、通义千问、Kimi。
· 同一个词查得越多，复习越靠前。每天间隔复习，配写作卡和口语卡。
"""

import csv
import datetime as dt
import json
import os
import pathlib
import re
import plistlib
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

APP_NAME = "浮词"
APP_DIR = os.path.join(os.path.expanduser("~"), "WordFloat")
DATA_FILE = os.path.join(APP_DIR, "words.json")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
BACKUP_DIR = os.path.join(APP_DIR, "backups")
BACKUP_KEEP = 7
# 开机启动：程序复制一份到 ~/WordFloat/app/，登录时由 launchd 打开（桌面文件夹受系统权限保护，launchd 读不到）
AGENT_LABEL = "com.wordfloat.app"
AGENT_PLIST = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", AGENT_LABEL + ".plist")
APP_COPY_DIR = os.path.join(APP_DIR, "app")

FREE_PRESET = "智谱 GLM-4.7-Flash（免费）"
# 服务名 → (接口地址, 模型, 申请 Key 的网址)
PRESETS = {
    FREE_PRESET: ("https://open.bigmodel.cn/api/paas/v4", "glm-4.7-flash",
                  "https://open.bigmodel.cn/"),
    "DeepSeek": ("https://api.deepseek.com", "deepseek-chat", "https://platform.deepseek.com/"),
    "通义千问": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
             "https://bailian.console.aliyun.com/"),
    "Kimi": ("https://api.moonshot.cn/v1", "moonshot-v1-8k", "https://platform.moonshot.cn/"),
}

# 主模型繁忙时依次换用的免费模型
FALLBACKS = {FREE_PRESET: ["glm-4-flash-250414", "glm-4-flash"]}

DEFAULT_CFG = {
    "preset": FREE_PRESET,
    "keys": {},
    "watch_clipboard": False,
    "ball": None,
    "autostart": True,
}

QUERY_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]*(?: [A-Za-z][A-Za-z'\-]*){0,5}$")
CLIP_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]*(?: [A-Za-z][A-Za-z'\-]*){0,3}$")
STRIP_CHARS = " \t\r\n.,;:!?\"“”‘()[]{}<>«»…—–"


def load_cfg():
    raw = load_json(CONFIG_FILE, {})
    cfg = dict(DEFAULT_CFG)
    cfg["keys"] = {}
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CFG or k in ("base_url", "model")})
        cfg["keys"] = dict(raw.get("keys") or {})
        # 旧版配置：单个 api_key
        old = (raw.get("api_key") or "").strip()
        if old and raw.get("preset") in PRESETS and raw.get("preset") not in cfg["keys"]:
            cfg["keys"][raw["preset"]] = old
    if cfg.get("preset") not in PRESETS:
        cfg["preset"] = FREE_PRESET
    # 旧版默认是 DeepSeek 但没填 Key：直接换成免费模型
    if not cfg["keys"].get(cfg["preset"]):
        cfg["preset"] = FREE_PRESET
    return cfg


def api_cfg(cfg):
    """把设置折算成一次调用需要的三样东西。"""
    base, model, _ = PRESETS[cfg["preset"]]
    return {"base_url": base, "model": model, "fallbacks": FALLBACKS.get(cfg["preset"], []),
            "api_key": (cfg.get("keys") or {}).get(cfg["preset"], "")}


# ───────────────────────── 基础工具 ─────────────────────────

def now():
    return dt.datetime.now()


def stamp(t=None):
    return (t or now()).isoformat(timespec="seconds")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        # 文件损坏：先改名备份，不覆盖用户数据
        try:
            os.replace(path, path + ".broken-" + now().strftime("%Y%m%d%H%M%S"))
        except Exception:
            pass
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def normalize(q):
    q = (q or "").replace("’", "'").replace("‘", "'")
    q = q.strip().strip(STRIP_CHARS).strip("'")
    return re.sub(r"\s+", " ", q)


def meaning_text(e):
    return "；".join(f"{s.get('pos', '')} {s.get('zh', '')}".strip() for s in e.get("senses") or [])


def open_path(p):
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", p])
        elif sys.platform.startswith("win"):
            os.startfile(p)  # noqa
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception:
        webbrowser.open(pathlib.Path(p).as_uri())


# ───────────────────────── 大模型接口 ─────────────────────────

class LLMError(Exception):
    pass


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def build_body(base, model, system, user, max_tokens, temperature):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if "bigmodel.cn" in base:
        body["thinking"] = {"type": "disabled"}  # GLM-4.7 默认会先“思考”，查词不需要，关掉更快
    return body


class _Busy(LLMError):
    """这个模型此刻用不了（繁忙或不存在），可以换下一个模型再试。"""


def _chat_once(url, key, base, model, system, user, max_tokens, temperature, timeout):
    data = json.dumps(build_body(base, model, system, user, max_tokens, temperature)).encode("utf-8")
    for attempt in range(2):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
                payload = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            if e.code in (429, 500, 502, 503) and attempt == 0:
                time.sleep(1.5)
                continue
            try:
                msg = json.loads(detail).get("error", {}).get("message") or ""
            except Exception:
                msg = detail[:120]
            if e.code == 401:
                raise LLMError("API Key 无效。请复制完整的 Key 重新设置（右键悬浮球 → 设置 API Key）。")
            if e.code == 402:
                raise LLMError("账户余额不足。")
            if e.code in (429, 500, 502, 503):
                raise _Busy("模型此刻繁忙。")
            if e.code in (400, 404) and ("model" in detail.lower() or "模型" in detail):
                raise _Busy("模型不可用。")
            raise LLMError(f"接口出错（{e.code}）{('：' + msg) if msg else ''}")
        except urllib.error.URLError as e:
            reason = str(e.reason)
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                raise LLMError("SSL 证书校验失败。重新双击「浮词.command」会自动修复。")
            raise LLMError(f"网络连不上：{reason}")
        except (socket.timeout, TimeoutError):
            raise _Busy("请求超时。")
        except json.JSONDecodeError:
            raise LLMError("接口返回的不是 JSON。")
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except Exception:
        raise LLMError("接口返回格式不认识：" + str(payload)[:200])


def chat(cfg, system, user, max_tokens=1200, temperature=0.3, timeout=60):
    """依次尝试主模型和备用模型；只有“繁忙/不可用”才换下一个。"""
    key = (cfg.get("api_key") or "").strip()
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    if not key:
        raise LLMError("还没填 API Key。右键悬浮球 → 设置 API Key。")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    models = [cfg.get("model")] + list(cfg.get("fallbacks") or [])
    last = None
    for m in [x for x in models if x]:
        try:
            return _chat_once(url, key, base, m, system, user, max_tokens, temperature, timeout)
        except _Busy as e:
            last = e
            continue
    raise LLMError("免费模型此刻繁忙，稍后自动重试。" if last else "没有可用的模型。")


def parse_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    cands = [t]
    if "{" in t and "}" in t:
        cands.append(t[t.find("{"): t.rfind("}") + 1])
    for c in cands:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    raise LLMError("模型没有按格式返回，再试一次。")


LOOKUP_SYSTEM = "你是严谨的英汉词典编辑，一律使用英式英语的拼写和用法（如 colour、organise、flat）。只输出一个 JSON 对象，不要任何解释，不要代码块标记。"
LOOKUP_PROMPT = """查询：{q}

按这个结构返回 JSON：
{{"lemma": "词典原形；短语保持原样；拼写有误时给最可能的正确拼写",
 "phonetic": "英式音标（RP），如 /ˈvɪvɪd/；短语可留空",
 "senses": [{{"pos": "词性缩写，如 n. / v. / adj. / phr.", "zh": "简明中文释义，同一词性的多个意思用；分隔"}}],
 "note": "一句中文记忆点：词根词缀或近义词辨析，二十字左右",
 "use": "一句中文“考试用法”：IELTS 写作/口语可直接套用的搭配或同义替换，或 GMAT 阅读里常见的语境义，30 字以内",
 "example": {{"en": "一个学术或议论文语体的英文例句，接近 GMAT 阅读或 IELTS 写作的风格，25 词以内", "zh": "例句的中文翻译"}}}}

senses 按常用程度排序，最多 3 条。"""


def lookup_word(cfg, q):
    res = parse_json(chat(cfg, LOOKUP_SYSTEM, LOOKUP_PROMPT.format(q=q), timeout=15))
    senses = []
    for s in res.get("senses") or []:
        if isinstance(s, dict) and s.get("zh"):
            senses.append({"pos": str(s.get("pos") or "").strip(), "zh": str(s["zh"]).strip()})
    ex = res.get("example") if isinstance(res.get("example"), dict) else {}
    return {
        "lemma": str(res.get("lemma") or q).strip() or q,
        "phonetic": str(res.get("phonetic") or "").strip(),
        "senses": senses[:3],
        "note": str(res.get("note") or "").strip(),
        "use": str(res.get("use") or "").strip(),
        "example": {"en": str(ex.get("en") or "").strip(), "zh": str(ex.get("zh") or "").strip()},
    }


# ───────────────────────── 数据 ─────────────────────────

class Store:
    def __init__(self):
        data = load_json(DATA_FILE, [])
        self.words = data if isinstance(data, list) else []

    def save(self):
        save_json(DATA_FILE, self.words)

    def get(self, key):
        for e in self.words:
            if e.get("key") == key:
                return e
        return None

    def find(self, q):
        ql = q.lower()
        for e in self.words:
            if e.get("key") == ql or ql in [f.lower() for f in e.get("forms", [])]:
                return e
        return None

    def add_pending(self, q):
        t = stamp()
        e = self.find(q)
        if e is not None:
            e["lookups"].append(t)
        else:
            e = {"key": q.lower(), "lemma": q, "forms": [], "phonetic": "", "senses": [],
                 "note": "", "example": {}, "added": t, "lookups": [t], "status": "pending"}
            self.words.append(e)
        self.save()
        return e

    def apply_result(self, q, res, count_lookup):
        t = stamp()
        lemma = res.get("lemma") or q
        key = lemma.lower()
        e = self.get(key)
        pend = self.find(q)
        if pend is e:
            pend = None
        if e is None:
            if pend is not None:
                e, pend = pend, None
                e["key"] = key
            else:
                e = {"key": key, "forms": [], "added": t, "lookups": []}
                self.words.append(e)
        if res.get("use"):
            e["use"] = res["use"]
        keep_note = e.get("note") if not res.get("note") else res.get("note")
        keep_ex = e.get("example") if not (res.get("example") or {}).get("en") else res.get("example")
        e.update(lemma=lemma, phonetic=res.get("phonetic", "") or e.get("phonetic", ""),
                 senses=res.get("senses", []) or e.get("senses", []),
                 note=keep_note or "", example=keep_ex or {}, status="ok")
        forms = e.setdefault("forms", [])
        if q.lower() != key and q.lower() not in [f.lower() for f in forms]:
            forms.append(q)
        if pend is not None:
            e["lookups"] = sorted(e["lookups"] + pend["lookups"])
            e["added"] = min(e["added"], pend["added"])
            self.words.remove(pend)
        if count_lookup:
            e["lookups"].append(t)
        self.save()
        return e

    def delete(self, key):
        self.words = [e for e in self.words if e.get("key") != key]
        self.save()

# ───────────────────────── 本地词典（ECDICT，MIT 许可） ─────────────────────────
#
# 释义先从本地词典取，毫秒级出结果；大模型只在后台补“记忆点 + 例句”。
# 词典随程序放在同一文件夹（wordfloat_dict.db.gz），首次启动解压到 ~/WordFloat/dict.db。

DICT_DB = os.path.join(APP_DIR, "dict.db")
DICT_GZ_NAME = "wordfloat_dict.db.gz"

_POS_FIX = {"a.": "adj.", "ad.": "adv.", "art.": "art.", "pl.": "n."}
_DOMAIN = re.compile(r"^\[(医|法|经|计|化|电|机|建|化学|物|数|生|地|植|动|军|天|农|矿|冶|纺|航|海|体|语|宗|心|史|哲|乐|美|印|测|无|铁|船|水|商|律|网络)\]")
_IPA = str.maketrans({"ә": "ə", "'": "ˈ", ",": "ˌ", "ɡ": "g", "ʌ": "ʌ"})
_INFLECT = re.compile(r"^(\w[\w\- ]*)的(过去式|过去分词|现在分词|复数|第三人称单数|比较级|最高级)")


class LocalDict:
    def __init__(self):
        self.db = None

    def ensure(self, script_dir):
        """需要时解压词典。返回是否可用。"""
        if self.db is not None:
            return True
        gz = os.path.join(script_dir, DICT_GZ_NAME)
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            if (not os.path.exists(DICT_DB) or
                    (os.path.exists(gz) and os.path.getmtime(gz) > os.path.getmtime(DICT_DB))):
                if not os.path.exists(gz):
                    return False
                import gzip
                import shutil
                tmp = DICT_DB + ".tmp"
                with gzip.open(gz, "rb") as fi, open(tmp, "wb") as fo:
                    shutil.copyfileobj(fi, fo, 1 << 20)
                os.replace(tmp, DICT_DB)
            import sqlite3
            self.db = sqlite3.connect(DICT_DB, check_same_thread=False)
            return True
        except Exception:
            self.db = None
            return False

    def _row(self, w):
        return self.db.execute("SELECT w, p, t, x FROM d WHERE w = ?", (w,)).fetchone()

    def lookup(self, q):
        """返回与 lookup_word 同结构的结果；查不到返回 None。"""
        if self.db is None:
            return None
        key = q.lower()
        try:
            row = self._row(key)
            form = None
            # 变形词：优先跳到原形
            if row is not None:
                lemma = None
                for part in (row[3] or "").split("/"):
                    if part.startswith("0:"):
                        lemma = part[2:].lower()
                m = _INFLECT.match(row[2] or "")
                if not lemma and m:
                    lemma = m.group(1).lower()
                if lemma and lemma != key:
                    base = self._row(lemma)
                    if base is not None:
                        form, row = key, base
            if row is None:
                f = self.db.execute("SELECT lemma FROM f WHERE form = ?", (key,)).fetchone()
                if f:
                    row = self._row(f[0])
                    form = key
            if row is None:
                return None
        except Exception:
            return None
        w, p, t, x = row
        return {
            "lemma": w,
            "phonetic": ("/" + p.translate(_IPA).strip() + "/") if p else "",
            "senses": parse_senses(t),
            "note": "",
            "example": {"en": "", "zh": ""},
            "source": "dict",
        }


def parse_senses(t):
    lines = [l.strip() for l in (t or "").split("\n") if l.strip()]
    main = [l for l in lines if not _DOMAIN.match(l)] or lines
    out = []
    for l in main[:3]:
        m = re.match(r"^([a-z]{1,6}\.)(\s*&\s*[a-z]{1,6}\.)?\s*(.*)$", l)
        if m:
            pos, zh = _POS_FIX.get(m.group(1), m.group(1)), m.group(3)
        else:
            pos, zh = "phr.", l
        zh = _DOMAIN.sub("", zh).strip()
        zh = re.sub(r"\s*,\s*", "；", zh)
        zh = re.sub(r"\s*;\s*", "；", zh)
        zh = "；".join([z for z in zh.split("；") if z.strip()][:4])     # 每个词性最多 4 个义项，保持简洁
        if zh:
            out.append({"pos": pos, "zh": zh})
    return out


# 雅思大作文高频话题（相关话题已合并）
TOPICS = ["教育", "科技与媒体", "环境与能源", "健康", "工作与经济", "政府与法律", "城市与交通", "文化与社会"]
TOPIC_ALIAS = {
    "儿童": "教育", "家庭教育": "教育",
    "科技": "科技与媒体", "媒体": "科技与媒体", "广告": "科技与媒体", "媒体与广告": "科技与媒体",
    "环境": "环境与能源", "能源": "环境与能源", "气候": "环境与能源",
    "体育": "健康", "运动": "健康",
    "工作": "工作与经济", "经济": "工作与经济", "全球化": "工作与经济", "商业": "工作与经济",
    "政府": "政府与法律", "犯罪": "政府与法律", "法律": "政府与法律", "犯罪与法律": "政府与法律",
    "城市化": "城市与交通", "交通": "城市与交通", "城市": "城市与交通", "住房": "城市与交通",
    "文化": "文化与社会", "艺术": "文化与社会", "文化与艺术": "文化与社会", "社会": "文化与社会",
    "家庭": "文化与社会", "家庭与社会": "文化与社会", "旅游": "文化与社会",
}


def norm_topic(t):
    t = (t or "").strip()
    t = TOPIC_ALIAS.get(t, t)
    return t if t in TOPICS else ""

ENRICH_SYSTEM = "你是英国的雅思写作与口语教练，一律使用英式英语的拼写、用词和口语习惯（如 colour、organise、flat、I reckon）。只输出一个 JSON 对象，不要任何解释，不要代码块标记。"
ENRICH_PROMPT = """单词：{w}
释义：{m}

返回：{{"note": "一句中文记忆点：词根词缀或近义辨析，二十字左右",
 "use": "一句中文“考试用法”：IELTS 写作/口语可直接套用的搭配或同义替换，或 GMAT 阅读里常见的语境义，30 字以内",
 "example": {{"en": "一个学术或议论文语体的英文例句，接近 GMAT 阅读或 IELTS 写作的风格，25 词以内", "zh": "例句的中文翻译"}},
 "topic": "这个词在雅思中最常出现的话题，从 {topics} 中选一个",
 "writing": {{"q": "一道能自然用到这个词的雅思大作文（Task 2）题目，英文，一句话",
              "model": "针对该题、用到这个词的 7 分以上论证句，英文，30 词以内",
              "zh": "范句的中文翻译",
              "chunks": ["从 model 里原样摘出 2–3 个最值得背的词块，每个 2–6 个词，与 model 一字不差：第一个是目标词所在的搭配（如 mitigate the environmental costs of），其余是可在别的作文里复用的表达或句式（如 while preserving、play a pivotal role in）"]}},
 "speaking": {{"q": "一道能自然用到这个词的雅思口语 Part 1 或 Part 3 问题，英文",
               "model": "用到这个词的自然英式口语回答，像英国人说话，可用 I'd say / to be honest / I reckon 等，英文，30 词以内",
               "zh": "回答的中文翻译"}}}}"""


def _pair(res, k, fields):
    d = res.get(k) if isinstance(res.get(k), dict) else {}
    return {f: str(d.get(f) or "").strip() for f in fields}


CHUNK_PROMPT = """目标词：{w}
雅思大作文范句：{s}

从范句里原样摘出 2–3 个最值得背的词块，每个 2–6 个词，必须与范句一字不差、互不重叠：
第一个是目标词所在的搭配（如 mitigate the environmental costs of），其余是可在别的作文里复用的表达或句式（如 while preserving、play a pivotal role in）。
返回：{{"chunks": ["...", "..."]}}"""


def clean_chunks(model, chunks):
    """只留范句里真实出现、2–6 词、互不重叠的词块，按出现顺序，最多 3 个；大小写取范句原文。"""
    model = model or ""
    low = model.lower()
    spans = []
    for c in chunks if isinstance(chunks, list) else []:
        c = re.sub(r"\s+", " ", str(c or "")).strip(" ,.;:")
        if not c or not 1 <= len(c.split()) <= 6:
            continue
        j = low.find(c.lower())
        if j < 0 or any(j < b and a < j + len(c) for a, b in spans):
            continue
        spans.append((j, j + len(c)))
    spans.sort()
    return [model[a:b] for a, b in spans[:3]]


def target_in(e, s):
    """句子里目标词（含变形）出现的那一段，找不到返回空。"""
    words = sorted({e.get("lemma", "")} | set(e.get("forms") or []), key=len, reverse=True)
    words = [w for w in words if w]
    if not words or not s:
        return ""
    m = re.search(r"\b(" + "|".join(re.escape(w) for w in words) + r")[a-z]{0,3}\b", s, re.I)
    return m.group(0) if m else ""


def chunks_for(e):
    """填空用的词块。AI 还没标出来时，先只挖目标词。"""
    w = e.get("writing") or {}
    model = w.get("model", "")
    got = clean_chunks(model, w.get("chunks"))
    if got:
        return got
    t = target_in(e, model)
    return [t] if t else []


def chunk_word(cfg, w, model):
    res = parse_json(chat(cfg, ENRICH_SYSTEM, CHUNK_PROMPT.format(w=w, s=model), max_tokens=200, timeout=20))
    return clean_chunks(model, res.get("chunks"))


def enrich_word(cfg, w, meaning):
    res = parse_json(chat(cfg, ENRICH_SYSTEM,
                          ENRICH_PROMPT.format(w=w, m=meaning or "（无）", topics="、".join(TOPICS)),
                          max_tokens=700, timeout=25))
    topic = str(res.get("topic") or "").strip()
    raw_w = res.get("writing") if isinstance(res.get("writing"), dict) else {}
    writing = _pair(res, "writing", ["q", "model", "zh"])
    return {"note": str(res.get("note") or "").strip(),
            "use": str(res.get("use") or "").strip(),
            "example": _pair(res, "example", ["en", "zh"]),
            "topic": norm_topic(topic),
            "writing": dict(writing, chunks=clean_chunks(writing["model"], raw_w.get("chunks"))),
            "speaking": _pair(res, "speaking", ["q", "model", "zh"])}


CHECK_SYSTEM = "你是英国的雅思考官兼教练，批改严格但简洁；改写和升级版一律用英式英语的拼写和用法，美式拼写不算错但改写时换成英式。只输出一个 JSON 对象，不要任何解释，不要代码块标记。"
CHECK_PROMPT = {
    "write": """目标词：{w}（{m}）
雅思大作文题目：{q}
考生写的句子：{t}

判断：考生是否正确使用了目标词（词义、搭配、语法），句子是否符合议论文书面语体。
返回：{{"ok": true 或 false（目标词用对且无明显语法错误为 true）,
 "corrected": "在考生原句基础上最小改动后的正确句子；原句已正确就原样返回",
 "better": "保留考生意思、用到目标词的 7 分以上版本，30 词以内",
 "why": "中文点评：指出最关键的一处问题或亮点，40 字以内",
 "tag": "如果原句有语言错误，从 {tags} 中选最主要的一类；没有错误填空字符串"}}""",
    "speak": """目标词：{w}（{m}）
雅思口语问题：{q}
考生的口头回答（语音转写，可能有识别误差，忽略标点和大小写）：{t}

判断：考生是否正确使用了目标词，表达是否自然、像口语。
返回：{{"ok": true 或 false（目标词用对且意思通顺为 true）,
 "corrected": "在考生原话基础上最小改动后的正确说法",
 "better": "保留考生意思、用到目标词的地道英式口语版本，可用 I'd say / to be honest / I reckon 等，30 词以内",
 "why": "中文点评：指出最关键的一处问题或亮点，40 字以内",
 "tag": "如果原话有语言错误，从 {tags} 中选最主要的一类；没有错误填空字符串"}}""",
}


def check_sentence(cfg, kind, w, meaning, q, text):
    res = parse_json(chat(cfg, CHECK_SYSTEM,
                          CHECK_PROMPT[kind].format(w=w, m=meaning or "", q=q or "（无）", t=text, tags="、".join(ERROR_TAGS)),
                          max_tokens=500, temperature=0.2, timeout=30))
    ok = res.get("ok")
    return {"ok": ok is True or str(ok).lower() == "true",
            "corrected": str(res.get("corrected") or "").strip(),
            "better": str(res.get("better") or "").strip(),
            "why": str(res.get("why") or "").strip(),
            "tag": norm_tag(res.get("tag"))}


# ───────────────────────── 错误本 · 每周练习 · GMAT 熟词僻义 ─────────────────────────

ERRORS_FILE = os.path.join(APP_DIR, "errors.json")
WEEKLY_FILE = os.path.join(APP_DIR, "weekly.json")
GMAT_FILE = os.path.join(APP_DIR, "gmat.json")
ERROR_TAGS = ["介词", "冠词", "时态", "主谓一致", "单复数", "词性", "搭配", "中式表达", "句子结构", "拼写", "语域", "其他"]
FIX_DAILY = 2          # 每天的改错卡
GMAT_DAILY = 3         # 每天的 GMAT 卡（含复习）
GMAT_DAILY_NEW = 2     # 其中新词最多几个
GMAT_BUFFER = 6        # 后台预先出好的题
GMAT_STEPS = [2, 5, 12, 30, 60]


def week_key(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def norm_tag(t):
    t = str(t or "").strip()
    if not t:
        return ""
    for tag in ERROR_TAGS:
        if tag in t:
            return tag
    return "其他"


def _norm_text(s):
    s = str(s or "").lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z' -]", " ", s)).strip()


def clean_errors(items):
    out = []
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict):
            continue
        orig, cor = str(x.get("orig") or "").strip(), str(x.get("corrected") or "").strip()
        if orig and cor and _norm_text(orig) != _norm_text(cor):
            out.append({"orig": orig, "corrected": cor, "tag": norm_tag(x.get("tag")) or "其他",
                        "why": str(x.get("why") or "").strip()})
    return out[:4]


def _band(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(9.0, round(v * 2) / 2))


WEEKLY_TASK_PROMPT = """话题：{topic}
学习者这周学的词：{words}

出两道雅思练习题，让学习者能自然用上这些词：
1. write：一道雅思大作文（Task 2）题目，英文，和话题相关，常见题型（同意与否 / 讨论双方 / 利弊 / 原因与对策）。
2. speak：一张雅思口语 Part 2 话题卡，英文：card 是 "Describe ..." 一句；points 是 3 条 "You should say" 要点，最后一条以 "and explain" 开头。
返回：{{"write": {{"q": "..."}}, "speak": {{"card": "Describe ...", "points": ["...", "...", "and explain ..."]}}}}"""

ASSESS_SYSTEM = ("你是英国的雅思考官，按官方公开的评分标准严格给分，不虚高；改写一律用英式英语的拼写和用法。"
                 "只输出一个 JSON 对象，不要任何解释，不要代码块标记。")
ASSESS_PROMPT = {
    "write": """雅思大作文题目：{q}
要求：写一个主体段（120–180 词），尽量用上：{items}
考生的段落：
{t}

按雅思写作评分标准给这个段落打分（0–9，0.5 一档，按单段表现估计）：
TR 任务回应、CC 连贯与衔接、LR 词汇丰富度、GRA 语法多样性与准确性。
返回：{{"bands": {{"TR": 6.5, "CC": 6, "LR": 6.5, "GRA": 6}}, "overall": 6.5,
 "tips": ["最重要的 3 条中文改进建议，每条 30 字以内，具体到哪句话"],
 "better": "保留考生论点、用上要求词的 7.5 分版本，英式英语，篇幅与原段相当",
 "errors": [{{"orig": "原文中有错的一句，原样摘录", "corrected": "最小改动后的正确句子", "tag": "从 {tags} 中选一个", "why": "中文说明，25 字以内"}}]}}
errors 最多 4 条，只列真正的语言错误。""",
    "speak": """雅思口语 Part 2 话题卡：{q}
尽量用上：{items}
考生的回答（语音转写，可能有识别误差，忽略标点和大小写）：
{t}

按雅思口语评分标准打分（0–9，0.5 一档）：FC 流利与连贯、LR 词汇、GRA 语法。发音无法从文字判断，不评。
返回：{{"bands": {{"FC": 6, "LR": 6, "GRA": 5.5}}, "overall": 6,
 "tips": ["最重要的 3 条中文改进建议，每条 30 字以内"],
 "better": "保留考生内容的地道英式口语版本，自然连贯，约 150 词",
 "errors": [{{"orig": "原话中有错的一句，原样摘录", "corrected": "最小改动后的正确说法", "tag": "从 {tags} 中选一个", "why": "中文说明，25 字以内"}}]}}
errors 最多 4 条，只列真正的语言错误。""",
}


def gen_weekly(cfg, topic, items):
    words = "、".join(f"{x['word']}（{x['chunk']}）" if x.get("chunk") else x["word"] for x in items)
    res = parse_json(chat(cfg, ENRICH_SYSTEM, WEEKLY_TASK_PROMPT.format(topic=topic or "综合", words=words),
                          max_tokens=400, temperature=0.7, timeout=40))
    w = res.get("write") if isinstance(res.get("write"), dict) else {}
    s = res.get("speak") if isinstance(res.get("speak"), dict) else {}
    points = [str(p).strip() for p in s.get("points") or [] if str(p).strip()][:4]
    out = {"write": str(w.get("q") or "").strip(), "card": str(s.get("card") or "").strip(), "points": points}
    if not out["write"] or not out["card"]:
        raise LLMError("出题失败，稍后再试")
    return out


def assess(cfg, kind, q, items, text):
    res = parse_json(chat(cfg, ASSESS_SYSTEM,
                          ASSESS_PROMPT[kind].format(q=q, items="、".join(items) or "（无）", t=text,
                                                     tags="、".join(ERROR_TAGS)),
                          max_tokens=1400, temperature=0.2, timeout=90))
    keys = ["TR", "CC", "LR", "GRA"] if kind == "write" else ["FC", "LR", "GRA"]
    raw = res.get("bands") if isinstance(res.get("bands"), dict) else {}
    bands = {k: _band(raw.get(k)) for k in keys}
    got = [v for v in bands.values() if v is not None]
    overall = _band(res.get("overall"))
    if overall is None and got:
        overall = _band(sum(got) / len(got))
    return {"bands": bands, "overall": overall,
            "tips": [str(t).strip() for t in res.get("tips") or [] if str(t).strip()][:3],
            "better": str(res.get("better") or "").strip(),
            "errors": clean_errors(res.get("errors"))}


# GMAT 阅读里常见的熟词僻义：(词, 要考的义项)
GMAT_WORDS = [
    ("qualify", "使（说法）有所保留；限定"), ("check", "抑制；阻止"), ("discriminate", "辨别；区分"),
    ("novel", "新颖的"), ("champion", "拥护；倡导"), ("sanction", "批准；认可"), ("temper", "缓和；调和"),
    ("appreciate", "升值"), ("arrest", "阻止；遏制（进程）"), ("bear out", "证实"), ("capital", "首要的；极其严重的"),
    ("command", "博得；赢得（尊重、价格）"), ("compound", "使加剧；使恶化"), ("concern", "企业；公司"),
    ("conviction", "坚定的信念"), ("credit", "把……归功于"), ("discount", "不重视；对……打折扣"),
    ("entertain", "考虑；怀有（想法）"), ("fashion", "方式；塑造"), ("figure", "估计；认为"), ("flag", "减弱；衰退"),
    ("grave", "严重的"), ("grounds", "理由；根据"), ("harbour", "怀有（想法、感情）"), ("hold", "认为；主张"),
    ("idle", "无根据的；无用的"), ("issue", "后代；发行"), ("lean", "贫乏的；收益少的"), ("level", "提出（指控、批评）"),
    ("maintain", "断言；坚持认为"), ("mount", "发起；组织（活动）"), ("observe", "遵守；评论说"),
    ("occasion", "引起；导致"), ("party", "当事方；一方"), ("pedestrian", "平庸的；乏味的"), ("plastic", "可塑的"),
    ("precipitate", "促成；使突然发生"), ("principal", "本金；主要的"), ("realise", "实现（收益）；变现"),
    ("reservation", "保留意见；疑虑"), ("scale", "攀登；按比例调整"), ("school", "学派"), ("second", "附议；支持"),
    ("shoulder", "承担（责任、费用）"), ("sound", "合理的；可靠的"), ("spare", "简朴的；简洁的"),
    ("stem", "遏制；阻止"), ("stock", "普通的；老套的"), ("strain", "品种；系"), ("subject", "使经受；使服从"),
    ("suit", "诉讼"), ("tender", "投标；提出"), ("trying", "恼人的；难熬的"), ("uniform", "一致的；统一的"),
    ("wanting", "不足的；欠缺的"), ("weather", "经受住；平安渡过"), ("yield", "产量；收益"), ("abandon", "放纵；尽情"),
    ("account for", "解释；占（比例）"), ("address", "处理；应对"), ("advance", "提出（观点）"), ("afford", "提供；给予"),
    ("anchor", "使稳固；作为支柱"), ("apparent", "表面上的；貌似的"), ("assume", "承担；呈现"),
    ("becoming", "合适的；得体的"), ("betray", "暴露；显示"), ("bolster", "支持；加强"), ("brook", "容忍"),
    ("channel", "引导；输送"), ("claim", "夺走（生命）"), ("coin", "创造（新词）"), ("colour", "影响（判断、看法）"),
    ("confound", "使困惑；推翻（预期）"), ("contract", "收缩；感染（疾病）"), ("count", "有分量；重要"),
    ("court", "招致；追求"), ("cultivate", "培养；建立（关系）"), ("dated", "过时的"), ("dispense with", "免除；省掉"),
    ("doctor", "篡改；伪造"), ("economy", "节约；节省"), ("engage", "吸引（注意）；雇用"), ("enlist", "争取（支持）"),
    ("express", "明确的；特意的"), ("fabric", "结构；构造"), ("fast", "牢固的；不褪色的"), ("foot", "支付（账单）"),
    ("forward", "促进；转交"), ("found", "创立；以……为基础"), ("frame", "构想；表述"), ("gross", "总的；毛的"),
    ("inform", "影响；渗透于"), ("intimate", "暗示；透露"), ("lay", "外行的；非专业的"), ("liberal", "大量的；不拘泥字面的"),
    ("marked", "显著的；明显的"), ("moment", "重要性"), ("nominal", "微不足道的；名义上的"), ("object", "反对"),
    ("overlook", "忽视；俯瞰"), ("plant", "工厂；设备"), ("prescribe", "规定；指定"), ("project", "预测；投射"),
    ("register", "显示；表现出"), ("resolve", "决心；坚定"), ("return", "收益；利润"), ("rival", "比得上；与……匹敌"),
    ("seasoned", "经验丰富的"), ("stake", "利害关系；股份"), ("standing", "长期的；常设的"), ("submit", "主张；认为"),
    ("subscribe to", "赞同；同意"), ("tap", "开发；利用"), ("tax", "使负重担；耗费"), ("telling", "有说服力的；能说明问题的"),
    ("transparent", "易识破的；明显的"), ("vehicle", "手段；工具"), ("want", "缺乏；不足"),
]

GMAT_PROMPT = """单词：{w}
要考的义项：{sense}（熟词僻义）

仿照 GMAT 阅读（Reading Comprehension）文风，写一段 2–3 句的学术短文，用这个词的上述义项（不是它最常见的意思），然后出一道词义题。
返回：{{"passage": "短文，英文，60 词以内，自然用到目标词（可变形）",
 "q": "In the passage, the word '{w}' most nearly means",
 "options": ["四个英文选项，一个是正确义项，至少一个是这个词最常见的意思作干扰"],
 "answer": 正确选项的序号（0 到 3 的整数）,
 "explain": "中文解析：这里是什么意思、为什么不是常见义，40 字以内"}}"""


def gen_gmat(cfg, w, sense):
    res = parse_json(chat(cfg, ENRICH_SYSTEM, GMAT_PROMPT.format(w=w, sense=sense),
                          max_tokens=500, temperature=0.6, timeout=30))
    opts = [str(o).strip() for o in res.get("options") or [] if str(o).strip()]
    try:
        ans = int(res.get("answer"))
    except (TypeError, ValueError):
        ans = -1
    passage = str(res.get("passage") or "").strip()
    if len(opts) != 4 or not 0 <= ans < 4 or not passage:
        raise LLMError("题目格式不对")
    return {"word": w, "sense": sense, "passage": passage,
            "q": str(res.get("q") or f"In the passage, the word '{w}' most nearly means").strip(),
            "options": opts, "answer": ans, "explain": str(res.get("explain") or "").strip()}


# ───────────────────────── 间隔重复 ─────────────────────────
#
# 每个词一个 step：下次间隔 = SRS_STEPS[step] 天。
# 记得 → step+1；忘了 → 回到 0、明天再来；又被查到 → 退两档、今天就复习。
# 难度随间隔上升：认 → 填词 → 填词块 → 回译 / 自己写。口语卡在每一级穿插。
#   step 0–2（1–4 天）  看英文想中文
#   step 3（7 天）      例句里填这个词
#   step 4（15 天）     范句词块填空
#   step 5+（30 天起）  回译、自己造句、口语轮换
# step ≥ MASTERED_STEP 视为已掌握。

SRS_STEPS = [1, 2, 4, 7, 15, 30, 60, 120]
PRODUCE_STEP = 3      # 间隔 7 天起：例句填词 / 口语
CLOZE_STEP = 4        # 间隔 15 天起：范句词块填空 / 口语
WRITE_STEP = 5        # 间隔 30 天起：回译 / 写作造句 / 口语
MASTERED_STEP = 6
DAILY_LIMIT = 20
PRACTICE_LIMIT = 5    # 写作卡 + 口语卡每天最多几张（它们比较花时间）
DAILY_PRACTICE = {"write": 2, "speak": 2}   # 每天保证的写作卡、口语卡数量


def today_str():
    return now().date().isoformat()


def srs_new():
    return {"step": 0, "due": today_str(), "reps": 0, "lapses": 0}


def srs_state(e):
    st = (e.get("srs") or {}).get("step", 0)
    return "mastered" if st >= MASTERED_STEP else "learning"


SLANG_FILE = os.path.join(APP_DIR, "slang.json")
SLANG_SYSTEM = "你是在伦敦生活多年的英语老师。只输出一个 JSON 对象，不要任何解释，不要代码块标记。"
SLANG_PROMPT = """今天是 {d}。给我一句地道、常用、不粗俗的英式俚语或口语表达（英国人日常真的会说的）。
不要和这些重复：{recent}
返回：{{"phrase": "短语本身", "meaning": "简洁的中文意思", "example": "一句自然的英式口语例句",
 "example_zh": "例句的中文翻译", "note": "中文说明使用场合或语气，20 字以内"}}"""


class Core:
    """查词、补全、复习。界面只负责展示；耗时操作在后台线程，结果通过 post 回到主线程。"""

    def __init__(self, post):
        self.post = post            # post(fn, *args)：在主线程执行
        self.cfg = load_cfg()
        self.store = Store()
        self.inflight = set()
        errs = load_json(ERRORS_FILE, [])
        self.errors = errs if isinstance(errs, list) else []
        wk = load_json(WEEKLY_FILE, {})
        self.weekly = wk if isinstance(wk, dict) else {}
        gm = load_json(GMAT_FILE, {})
        self.gmat = gm if isinstance(gm, dict) else {}
        self.filling = False
        self.enriching = set()
        self.dict = LocalDict()
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        threading.Thread(target=self.dict.ensure, args=(self.script_dir,), daemon=True).start()

    def save_cfg(self):
        save_json(CONFIG_FILE, self.cfg)

    def has_key(self):
        return bool(api_cfg(self.cfg)["api_key"])

    # 查词。done(entry, error, cached)；enriched(entry) 在后台补齐记忆点和例句后调用
    def lookup(self, text, done, enriched=None):
        q = normalize(text)
        if not q or len(q) > 60 or not QUERY_RE.match(q):
            done(None, "只收英文单词或短语（最多 6 个词）。", False)
            return False
        e = self.store.find(q)
        if e is not None and e.get("status") == "ok":
            e["lookups"].append(stamp())
            self.srs_touch(e)
            done(e, None, True)
            self.enrich(e, enriched)
            return True
        # 本地词典：立刻出结果
        local = self.dict.lookup(q) if self.dict.db is not None else None
        if local is not None and local["senses"]:
            e = self.store.apply_result(q, local, count_lookup=True)
            self.srs_touch(e)
            done(e, None, False)
            self.enrich(e, enriched)
            return True
        if q.lower() in self.inflight:
            return False
        self.inflight.add(q.lower())
        cfg = api_cfg(self.cfg)

        def work():
            try:
                res, err = lookup_word(cfg, q), None
            except Exception as ex:
                res, err = None, str(ex)
            self.post(self._lookup_done, q, res, err, done)
        threading.Thread(target=work, daemon=True).start()
        return True

    def add_word(self, text):
        """网页右键加词。本地词典或词本里有的立刻返回；要问大模型的先返回 pending。"""
        box = {}
        self.lookup(text, lambda e, err, cached: box.update(e=e, err=err, cached=cached))
        if "e" not in box:
            return {"pending": True}
        e = box["e"]
        if e is None:
            return {"error": box["err"] or "没查到"}
        return {"word": e["lemma"], "meaning": meaning_text(e), "known": bool(box["cached"]),
                "key": e["key"]}

    def _lookup_done(self, q, res, err, done):
        self.inflight.discard(q.lower())
        if res is not None:
            e = self.store.apply_result(q, res, count_lookup=True)
            self.srs_touch(e)
            done(e, None, False)
        else:
            e = self.store.add_pending(q)
            self.srs_touch(e)
            done(e, err, False)

    # —— 间隔重复 ——
    def srs_touch(self, e, relook=True):
        """查到一次：新词进入复习；已有的词退两档、今天复习。"""
        if e is None:
            return
        if "srs" not in e:
            e["srs"] = srs_new()
        elif relook:
            s = e["srs"]
            s["step"] = max(0, s.get("step", 0) - 2)
            s["due"] = today_str()
        self.store.save()

    def due_entries(self):
        t = today_str()
        out = []
        for e in self.store.words:
            if e.get("status") != "ok" or not e.get("senses"):
                continue
            s = e.setdefault("srs", srs_new())
            if s.get("due", t) <= t:
                out.append(e)
        out.sort(key=lambda e: (-len(e.get("lookups", [])), e["srs"].get("due", ""), e.get("added", "")))
        return out

    def can_mode(self, e, m):
        w, sp = e.get("writing") or {}, e.get("speaking") or {}
        return {"produce": True, "recall": True,
                "cloze": bool(w.get("model") and chunks_for(e)),
                "translate": bool(w.get("model") and w.get("zh")),
                "write": bool(w.get("q")), "speak": bool(sp.get("q"))}.get(m, False)

    def pick_mode(self, e):
        s = e.get("srs") or srs_new()
        step, reps = s.get("step", 0), s.get("reps", 0)
        if step >= WRITE_STEP:
            order = [["write", "translate", "speak"], ["translate", "speak", "write"],
                     ["speak", "write", "translate"]][reps % 3]
        elif step >= CLOZE_STEP:
            order = ["cloze", "speak"] if reps % 2 == 0 else ["speak", "cloze"]
        elif step >= PRODUCE_STEP:
            order = ["produce", "speak"] if reps % 2 == 0 else ["speak", "produce"]
        else:
            return "recall"
        for m in order + ["translate", "cloze"]:
            if self.can_mode(e, m):
                return m
        return "produce"

    def quiet_mode(self, e):
        """写作 / 口语卡今天已练够：换成同一级别里不需要 AI 批改的卡。"""
        step = (e.get("srs") or {}).get("step", 0)
        for m in (["translate", "cloze"] if step >= WRITE_STEP else ["cloze"] if step >= CLOZE_STEP else []):
            if self.can_mode(e, m):
                return m
        return "produce"

    def card(self, e, mode=None):
        s = e.get("srs") or srs_new()
        ex = dict(e.get("example") or {})
        mine = e.get("mine") or []
        stem = e["lemma"].lower()[:max(3, len(e["lemma"]) - 2)]
        for m in reversed(mine):                      # 优先用自己写过的句子当例句
            if stem in m.get("text", "").lower():
                ex = {"en": m["text"], "zh": ""}
                break
        w, sp = e.get("writing") or {}, e.get("speaking") or {}
        return {
            "key": e["key"], "word": e["lemma"], "forms": e.get("forms", []),
            "phonetic": e.get("phonetic", ""),
            "senses": [{"pos": pos_label(x.get("pos")), "zh": x.get("zh", "")} for x in e.get("senses") or []],
            "meaning": meaning_text(e),
            "note": e.get("note", ""), "use": e.get("use", ""),
            "ex_en": ex.get("en", ""), "ex_zh": ex.get("zh", ""),
            "topic": norm_topic(e.get("topic")),
            "w_q": w.get("q", ""), "w_model": w.get("model", ""), "w_zh": w.get("zh", ""),
            "w_chunks": chunks_for(e),
            "s_q": sp.get("q", ""), "s_model": sp.get("model", ""), "s_zh": sp.get("zh", ""),
            "mine": mine,
            "count": len(e.get("lookups", [])), "step": s.get("step", 0),
            "mode": mode or self.pick_mode(e),
            "state": srs_state(e),
        }

    def practice_left(self):
        log = (self.cfg.get("practice_log") or {}).get(today_str(), {})
        return {k: max(0, n - log.get(k, 0)) for k, n in DAILY_PRACTICE.items()}

    def has_material(self, e, kind):
        return bool((e.get("writing" if kind == "write" else "speaking") or {}).get("q"))

    def queue(self, limit=DAILY_LIMIT):
        due = self.due_entries()[:limit]
        left = self.practice_left()
        cards, used = [], {"write": 0, "speak": 0}
        for e in due:
            m = self.pick_mode(e)
            if m in ("write", "speak"):
                if used[m] < left[m]:
                    used[m] += 1
                else:
                    m = self.quiet_mode(e)
            cards.append(self.card(e, m))
        # 当天的写作 / 口语练习不够：从学过但最久没练的词里补（只练习，不改变复习进度）
        in_queue = {c["key"] for c in cards}
        practiced = set()
        extras, tail = [], []
        for kind in ("write", "speak"):
            need = left[kind] - used[kind]
            if need <= 0:
                continue
            pool = [e for e in self.store.words
                    if e.get("status") == "ok" and e["key"] not in practiced and self.has_material(e, kind)]
            # 先用今天不在复习里的词；不够再用今天要复习的词（先回想一遍，最后再练）
            pool.sort(key=lambda e: (e["key"] in in_queue, e.get("practiced", ""),
                                     -len(e.get("lookups", [])), e.get("added", "")))
            for e in pool[:need]:
                practiced.add(e["key"])
                c = self.card(e, kind)
                c["extra"] = True
                (tail if e["key"] in in_queue else extras).append(c)
        extras += [self.fix_card(x) for x in self.fix_today()]
        extras += [self.gmat_card(g) for g in self.gmat_today()]
        for k, c in enumerate(extras):                 # 练习卡穿插在普通卡之间
            cards.insert(min(len(cards), 2 + k * 3), c)
        cards.extend(tail)
        self.store.save()
        return {"cards": cards, "due": len(self.due_entries())}

    # 造句 / 口语批改：prepare 和 save 在主线程，调用模型在请求线程
    def check_prepare(self, key, kind):
        e = self.store.get(key)
        if e is None:
            raise ValueError("词不存在")
        q = (e.get("writing" if kind == "write" else "speaking") or {}).get("q", "")
        return {"w": e["lemma"], "m": meaning_text(e), "q": q, "cfg": api_cfg(self.cfg)}

    def save_mine(self, key, kind, q, text, orig="", better="", why="", ok=False):
        """保存一次批改：原句、修改后、更好的版本、点评。下次做同一张卡时拿出来对照。"""
        e = self.store.get(key)
        if e is None or not text:
            return
        mine = [m for m in e.get("mine") or [] if m.get("text") != text]   # 同一句只留最新一次
        mine.append({"kind": kind, "q": q, "text": text, "orig": orig if orig != text else "",
                     "better": better, "why": why, "ok": bool(ok), "t": stamp()})
        e["mine"] = mine[-6:]
        self.store.save()

    def grade(self, key, ok, kind="", extra=False):
        if kind in ("fix", "gmat"):
            log = self.cfg.setdefault("practice_log", {}).setdefault(today_str(), {})
            log[kind] = log.get(kind, 0) + 1
            self.save_cfg()
            k = key.split(":", 1)[-1]
            return self.fix_grade(k, ok) if kind == "fix" else self.gmat_grade(k, ok)
        e = self.store.get(key)
        if e is None:
            return {"ok": False}
        if kind in ("write", "speak"):
            t = today_str()
            log = self.cfg.setdefault("practice_log", {})
            for d in [d for d in log if d < (now().date() - dt.timedelta(days=30)).isoformat()]:
                del log[d]
            day = log.setdefault(t, {})
            day[kind] = day.get(kind, 0) + 1
            self.save_cfg()
            e["practiced"] = t
        if extra:                                      # 额外练习：不改变复习进度
            self.store.save()
            return {"ok": True, "extra": True}
        s = e.setdefault("srs", srs_new())
        s["reps"] = s.get("reps", 0) + 1
        if ok:
            step = s.get("step", 0)
            days = SRS_STEPS[min(step, len(SRS_STEPS) - 1)]
            s["step"] = min(step + 1, len(SRS_STEPS))
        else:
            s["step"] = 0
            s["lapses"] = s.get("lapses", 0) + 1
            days = 1
        s["due"] = (now().date() + dt.timedelta(days=days)).isoformat()
        s["last"] = stamp()
        self.store.save()
        return {"ok": True, "due": s["due"], "days": days, "state": srs_state(e)}

    def stats(self):
        t = today_str()
        tomorrow = (now().date() + dt.timedelta(days=1)).isoformat()
        ok = [e for e in self.store.words if e.get("status") == "ok" and e.get("senses")]
        due = sum(1 for e in ok if (e.get("srs") or {}).get("due", t) <= t)
        nxt = sum(1 for e in ok if (e.get("srs") or {}).get("due", t) == tomorrow)
        mastered = sum(1 for e in ok if srs_state(e) == "mastered")
        left = self.practice_left()
        practice = sum(min(left[k], sum(1 for e in ok if self.has_material(e, k))) for k in left)
        practice += len(self.fix_today()) + len(self.gmat_today())
        return {"total": len(ok), "due": due, "tomorrow": nxt, "practice": practice,
                "errors_top": self.error_summary(), "weekly": self.weekly_status(),
                "mastered": mastered, "learning": len(ok) - mastered,
                "topics": {t: sum(1 for e in ok if norm_topic(e.get("topic")) == t) for t in TOPICS}}

    def book(self):
        rows = []
        for e in self.store.words:
            if e.get("status") != "ok":
                continue
            c = self.card(e)
            c["meaning"] = meaning_text(e)
            c["added"] = e.get("added", "")[:10]
            rows.append(c)
        rows.sort(key=lambda c: (c["state"] == "mastered", -c["count"], c["step"], c["word"]))
        return {"words": rows, "stats": self.stats()}

    def slang_today(self):
        """每日 slang：当天第一次请求时生成并缓存，不与最近 60 条重复。在请求线程里运行。"""
        if not hasattr(self, "_slang_lock"):
            self._slang_lock = threading.Lock()
        with self._slang_lock:
            data = load_json(SLANG_FILE, {"history": []})
            hist = data.get("history") or []
            t = today_str()
            for h in hist:
                if h.get("date") == t:
                    return h
            if not self.has_key():
                return None
            recent = "、".join(h.get("phrase", "") for h in hist[-60:]) or "（无）"
            res = parse_json(chat(api_cfg(self.cfg), SLANG_SYSTEM, SLANG_PROMPT.format(d=t, recent=recent),
                                  max_tokens=300, temperature=0.9, timeout=25))
            item = {k: str(res.get(k) or "").strip() for k in ("phrase", "meaning", "example", "example_zh", "note")}
            if not item["phrase"]:
                return None
            item["date"] = t
            hist.append(item)
            save_json(SLANG_FILE, {"history": hist[-300:]})
            return item

    def needs_enrich(self, e):
        return (e is not None and e.get("status") == "ok"
                and not (e.get("use") and (e.get("writing") or {}).get("q")))

    def enrich(self, e, enriched=None):
        """后台补记忆点和例句（本地词典没有这两项）。"""
        if not self.needs_enrich(e) or not self.has_key() or e["key"] in self.enriching:
            return
        self.enriching.add(e["key"])
        cfg = api_cfg(self.cfg)
        key, lemma, meaning = e["key"], e["lemma"], meaning_text(e)

        def work():
            try:
                res = enrich_word(cfg, lemma, meaning)
            except Exception:
                res = None
            self.post(self._enrich_done, key, res, enriched)
        threading.Thread(target=work, daemon=True).start()

    def needs_chunks(self, e):
        w = (e or {}).get("writing") or {}
        return e.get("status") == "ok" and bool(w.get("model")) and "chunks" not in w

    def enrich_batch(self, entries, gmat=()):
        """逐个补齐（免费模型只允许一个并发）：缺内容的整词补全，只缺词块的单独标词块，再出 GMAT 题。"""
        todo = [("gmat", "gmat:" + w, w, sense) for w, sense in gmat if "gmat:" + w not in self.enriching]
        for e in entries:
            if e["key"] in self.enriching:
                continue
            if self.needs_enrich(e):
                todo.append(("full", e["key"], e["lemma"], meaning_text(e)))
            elif self.needs_chunks(e):
                todo.append(("chunks", e["key"], e["lemma"], e["writing"]["model"]))
        if not todo or not self.has_key():
            return
        for _, k, _, _ in todo:
            self.enriching.add(k)
        cfg = api_cfg(self.cfg)

        def work():
            for kind, key, lemma, arg in todo:
                if kind == "gmat":
                    try:
                        item = gen_gmat(cfg, lemma, arg)
                    except Exception:
                        item = None
                    self.post(self._gmat_done, lemma, item)
                elif kind == "full":
                    try:
                        res = enrich_word(cfg, lemma, arg)
                    except Exception:
                        res = None
                    self.post(self._enrich_done, key, res, None)
                else:
                    try:
                        got = chunk_word(cfg, lemma, arg)
                    except Exception:
                        got = None                 # 网络或繁忙：下次再试
                    self.post(self._chunks_done, key, arg, got)
        threading.Thread(target=work, daemon=True).start()

    def _chunks_done(self, key, model, got):
        self.enriching.discard(key)
        e = self.store.get(key)
        if e is None or got is None:
            return
        w = e.get("writing") or {}
        if w.get("model") == model:               # 范句没被换掉才写回
            w["chunks"] = got                      # 标不出来也记成 []，免得反复请求
            self.store.save()

    def _enrich_done(self, key, res, enriched):
        self.enriching.discard(key)
        e = self.store.get(key)
        if e is None or not res:
            return
        e["note"] = res["note"] or e.get("note", "")
        e["use"] = res.get("use", "") or e.get("use", "")
        if res["example"].get("en"):
            e["example"] = res["example"]
        for k in ("topic", "writing", "speaking"):
            if res.get(k) and (not isinstance(res[k], dict) or res[k].get("q")):
                e[k] = res[k]
        self.store.save()
        if enriched:
            enriched(e)

    def fill_pending(self):
        if self.filling or not self.has_key():
            return
        for e in [e for e in self.store.words if e.get("status") == "pending"]:
            local = self.dict.lookup(e["lemma"])
            if local is not None and local["senses"]:
                self.store.apply_result(e["lemma"], local, count_lookup=False)
        full = [e for e in self.store.words if self.needs_enrich(e)]
        # 词块：快到填空那一级的词先补
        chunk = sorted([e for e in self.store.words if not self.needs_enrich(e) and self.needs_chunks(e)],
                       key=lambda e: -(e.get("srs") or {}).get("step", 0))
        self.enrich_batch((full + chunk)[:30], self.gmat_jobs())
        pend = [e["lemma"] for e in self.store.words if e.get("status") == "pending"]
        if not pend:
            return
        self.filling = True
        cfg = api_cfg(self.cfg)

        def work():
            for q in pend:
                try:
                    res = lookup_word(cfg, q)
                    self.post(self.store.apply_result, q, res, False)
                except Exception:
                    pass
            self.post(setattr, self, "filling", False)
        threading.Thread(target=work, daemon=True).start()

    # ───── 每日额度（写作、口语、改错、GMAT 共用一本账）─────
    def done_today(self, kind):
        return ((self.cfg.get("practice_log") or {}).get(today_str(), {})).get(kind, 0)

    # ───── 错误本 ─────
    def add_errors(self, items, key="", word="", src=""):
        known = {_norm_text(x.get("orig")) for x in self.errors}
        tomorrow = (now().date() + dt.timedelta(days=1)).isoformat()
        n = 0
        for x in clean_errors(items):
            if _norm_text(x["orig"]) in known:
                continue
            known.add(_norm_text(x["orig"]))
            n += 1
            self.errors.append(dict(x, id=f"{int(time.time() * 1000):x}{n}", key=key, word=word, src=src,
                                    t=stamp(), due=tomorrow, done=False))
        if n:
            del self.errors[:-400]
            save_json(ERRORS_FILE, self.errors)
        return n

    def error_summary(self, days=30):
        since = (now() - dt.timedelta(days=days)).isoformat()
        counts = {}
        for x in self.errors:
            if x.get("t", "") >= since:
                counts[x["tag"]] = counts.get(x["tag"], 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
        return [{"tag": t, "n": n} for t, n in top]

    def fix_today(self):
        left = max(0, FIX_DAILY - self.done_today("fix"))
        t = today_str()
        due = [x for x in self.errors if not x.get("done") and x.get("due", t) <= t]
        due.sort(key=lambda x: x.get("t", ""))
        return due[:left]

    def fix_card(self, x):
        return {"key": "fix:" + x["id"], "mode": "fix", "word": x.get("word", ""), "forms": [],
                "tag": x["tag"], "orig": x["orig"], "corrected": x["corrected"], "why": x.get("why", ""),
                "topic": ""}

    def fix_grade(self, eid, ok):
        for x in self.errors:
            if x["id"] == eid:
                x["reps"] = x.get("reps", 0) + 1
                if ok:
                    x["done"] = True
                else:
                    x["due"] = (now().date() + dt.timedelta(days=2)).isoformat()
                save_json(ERRORS_FILE, self.errors)
                return {"ok": True}
        return {"ok": False}

    def error_book(self):
        rows = sorted(self.errors, key=lambda x: x.get("t", ""), reverse=True)
        return {"errors": rows, "top": self.error_summary(), "open": sum(1 for x in rows if not x.get("done"))}

    # ───── GMAT 熟词僻义 ─────
    def gmat_today(self):
        left = max(0, GMAT_DAILY - self.done_today("gmat"))
        if not left:
            return []
        t = today_str()
        seen = [g for g in self.gmat.values() if g.get("first")]
        due = sorted([g for g in seen if g.get("due", t) <= t], key=lambda g: g.get("due", ""))
        new_left = max(0, GMAT_DAILY_NEW - sum(1 for g in seen if g.get("first") == t))
        order = {w: i for i, (w, _) in enumerate(GMAT_WORDS)}
        new = sorted([g for g in self.gmat.values() if not g.get("first")], key=lambda g: order.get(g["word"], 999))
        return (due + new[:new_left])[:left]

    def gmat_card(self, g):
        return {"key": "gmat:" + g["word"], "mode": "gmat", "word": g["word"], "forms": [],
                "sense": g["sense"], "passage": g["passage"], "q": g["q"], "options": g["options"],
                "answer": g["answer"], "explain": g.get("explain", ""), "topic": ""}

    def gmat_grade(self, w, ok):
        g = self.gmat.get(w)
        if g is None:
            return {"ok": False}
        g.setdefault("first", today_str())
        step = g.get("step", 0)
        if ok:
            days = GMAT_STEPS[min(step, len(GMAT_STEPS) - 1)]
            g["step"] = step + 1
        else:
            days, g["step"] = 1, 0
        g["due"] = (now().date() + dt.timedelta(days=days)).isoformat()
        save_json(GMAT_FILE, self.gmat)
        return {"ok": True, "due": g["due"]}

    def gmat_jobs(self):
        fresh = sum(1 for g in self.gmat.values() if not g.get("first"))
        need = GMAT_BUFFER - fresh
        return [(w, s) for w, s in GMAT_WORDS if w not in self.gmat][:max(0, need)]

    def _gmat_done(self, w, item):
        self.enriching.discard("gmat:" + w)
        if item:
            self.gmat[w] = item
            save_json(GMAT_FILE, self.gmat)

    # ───── 每周练习 ─────
    def week_words(self):
        """本周查过或复习过的词，按本周遇到次数排；不够就用最近加的词补。"""
        wk = week_key(now().date())

        def in_week(ts):
            try:
                return week_key(dt.datetime.fromisoformat(ts).date()) == wk
            except Exception:
                return False
        ok = [e for e in self.store.words if e.get("status") == "ok" and e.get("senses")]
        scored = []
        for e in ok:
            n = sum(1 for t in e.get("lookups", []) if in_week(t))
            if n or in_week((e.get("srs") or {}).get("last", "")):
                scored.append((n, e))
        scored.sort(key=lambda x: (-x[0], -len(x[1].get("lookups", []))))
        picked = [e for _, e in scored]
        if len(picked) < 8:
            rest = sorted([e for e in ok if e not in picked], key=lambda e: e.get("added", ""), reverse=True)
            picked += rest[:8 - len(picked)]
        return picked[:8]

    def weekly_prepare(self):
        wk = week_key(now().date())
        d = self.weekly.get(wk) or {}
        if d.get("task"):
            return {"week": wk, "ready": True}
        words = self.week_words()
        if len(words) < 3:
            return {"week": wk, "ready": False, "few": True}

        def item(e):
            ch = chunks_for(e)
            return {"word": e["lemma"], "forms": e.get("forms", []), "chunk": ch[0] if ch and ch[0] != e["lemma"] else ""}
        with_chunk = [e for e in words if (e.get("writing") or {}).get("model")]
        w_items = [item(e) for e in (with_chunk + [e for e in words if e not in with_chunk])[:4]]
        rest = [e for e in words if e["lemma"] not in {x["word"] for x in w_items}]
        s_items = [item(e) for e in (rest + words)[:4]]
        topics = [norm_topic(e.get("topic")) for e in words if norm_topic(e.get("topic"))]
        topic = max(set(topics), key=topics.count) if topics else ""
        return {"week": wk, "ready": False, "topic": topic, "w_items": w_items, "s_items": s_items,
                "cfg": api_cfg(self.cfg)}

    def weekly_set_task(self, wk, task, prep):
        d = self.weekly.setdefault(wk, {})
        d["task"] = dict(task, topic=prep["topic"], w_items=prep["w_items"], s_items=prep["s_items"])
        save_json(WEEKLY_FILE, self.weekly)

    def weekly_view(self):
        wk = week_key(now().date())
        d = self.weekly.get(wk) or {}
        hist = []
        for k in sorted(self.weekly)[-8:]:
            v = self.weekly[k]
            hist.append({"week": k, "write": (v.get("write") or {}).get("result", {}).get("overall"),
                         "speak": (v.get("speak") or {}).get("result", {}).get("overall")})
        return {"week": wk, "task": d.get("task"), "write": d.get("write"), "speak": d.get("speak"), "history": hist}

    def weekly_assess_prepare(self, kind):
        wk = week_key(now().date())
        task = (self.weekly.get(wk) or {}).get("task")
        if not task:
            raise ValueError("本周题目还没生成")
        items = task["w_items"] if kind == "write" else task["s_items"]
        q = task["write"] if kind == "write" else task["card"] + " You should say: " + "; ".join(task["points"])
        return {"week": wk, "q": q, "items": [x["word"] for x in items], "cfg": api_cfg(self.cfg)}

    def weekly_save(self, wk, kind, text, result):
        d = self.weekly.setdefault(wk, {})
        d[kind] = {"text": text, "result": result, "t": stamp()}
        save_json(WEEKLY_FILE, self.weekly)
        self.add_errors(result.get("errors"), src="weekly-" + kind)

    def weekly_status(self):
        d = self.weekly.get(week_key(now().date())) or {}
        return {k: ((d.get(k) or {}).get("result") or {}).get("overall") for k in ("write", "speak")}

    def export_csv(self):
        path = os.path.join(APP_DIR, f"words-{now():%Y%m%d}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["word", "phonetic", "meaning", "note", "example", "example_zh",
                        "first_seen", "times_seen"])
            for e in sorted(self.store.words, key=lambda e: e.get("added", "")):
                ex = e.get("example") or {}
                w.writerow([e["lemma"], e.get("phonetic", ""), meaning_text(e), e.get("note", ""),
                            ex.get("en", ""), ex.get("zh", ""), e.get("added", "")[:10],
                            len(e.get("lookups", []))])
        return path

    def ping(self, done):
        cfg = api_cfg(self.cfg)

        def work():
            try:
                chat(cfg, "只回答 OK", "ping", max_tokens=8, timeout=30)
                self.post(done, None)
            except Exception as ex:
                self.post(done, str(ex))
        threading.Thread(target=work, daemon=True).start()


def clip_word(s):
    q = normalize(s or "")
    return q if q and len(q) <= 40 and CLIP_RE.match(q) else None


# ───────────────────────── 备份与开机启动 ─────────────────────────

_BACKUP_RE = re.compile(r"^words-\d{4}-\d{2}-\d{2}\.json$")


def backup_words(keep=BACKUP_KEEP):
    """每天第一次运行时把 words.json 复制一份，只留最近 keep 天。返回今天的备份路径或 None。"""
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None                      # 没有文件或文件已损坏：不备份，免得挤掉好的备份
    if not isinstance(data, list) or not data:
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"words-{today_str()}.json")
    if not os.path.exists(path):
        tmp = path + ".tmp"
        shutil.copy2(DATA_FILE, tmp)
        os.replace(tmp, path)
    olds = sorted(f for f in os.listdir(BACKUP_DIR) if _BACKUP_RE.match(f))
    for f in olds[:-keep]:
        try:
            os.remove(os.path.join(BACKUP_DIR, f))
        except OSError:
            pass
    return path


def autostart_on():
    return os.path.exists(AGENT_PLIST)


def install_autostart(script=None, python=None):
    """登录时自动打开浮词。每次从桌面启动都会刷新那份副本，保证开机打开的是最新版。"""
    script = os.path.abspath(script or __file__)
    python = python or sys.executable
    os.makedirs(APP_COPY_DIR, exist_ok=True)
    dst = os.path.join(APP_COPY_DIR, "wordfloat.py")
    if os.path.realpath(script) != os.path.realpath(dst):
        tmp = dst + ".tmp"
        shutil.copy2(script, tmp)
        os.replace(tmp, dst)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [python, dst],
        "WorkingDirectory": APP_COPY_DIR,
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": os.path.join(APP_COPY_DIR, "wordfloat.log"),
        "StandardErrorPath": os.path.join(APP_COPY_DIR, "wordfloat.log"),
    }
    data = plistlib.dumps(plist)
    try:
        with open(AGENT_PLIST, "rb") as f:
            if f.read() == data:
                return AGENT_PLIST
    except OSError:
        pass
    os.makedirs(os.path.dirname(AGENT_PLIST), exist_ok=True)
    with open(AGENT_PLIST, "wb") as f:
        f.write(data)
    return AGENT_PLIST


def remove_autostart():
    # 只删配置，不 bootout：正在运行的浮词可能就是它启动的
    try:
        os.remove(AGENT_PLIST)
    except FileNotFoundError:
        pass


# ───────────────────────── 复习与生词本（本机网页） ─────────────────────────
#
# 浮词在 127.0.0.1 上开一个只供本机访问的小服务，页面在浏览器里打开，
# 复习结果直接写回词本。

SERVER_PORT = 47821

APP_CSS = """
:root{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--fg2:#6e6e73;--fg3:#aeaeb2;--line:rgba(0,0,0,.08);
--accent:#0071e3;--accent-soft:rgba(0,113,227,.10);--ok:#34c759;--no:#ff3b30;--fill:#e8e8ed;--shadow:0 1px 2px rgba(0,0,0,.04),0 8px 28px rgba(0,0,0,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--fg:#f5f5f7;--fg2:#a1a1a6;--fg3:#636366;
--line:rgba(255,255,255,.1);--accent:#2997ff;--accent-soft:rgba(41,151,255,.16);--ok:#30d158;--no:#ff453a;--fill:#2c2c2e;--shadow:none}}
*{box-sizing:border-box;margin:0}
html{-webkit-font-smoothing:antialiased}
body{background:var(--bg);color:var(--fg);font:17px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif}
a{color:var(--accent);text-decoration:none}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:16px;padding:14px 24px;
background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px)}
.top .title{font-weight:600;font-size:15px}
.top .sp{flex:1}
.top a,.top .meta{font-size:14px}
.meta{color:var(--fg2)}
.progress{flex:1;height:4px;border-radius:2px;background:var(--fill);overflow:hidden;max-width:420px}
.progress i{display:block;height:100%;width:0;background:var(--accent);border-radius:2px;transition:width .35s cubic-bezier(.22,1,.36,1)}
.speak{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:16px;color:var(--accent);background:var(--accent-soft);vertical-align:middle}
.speak svg{width:16px;height:16px}
mark{background:none;color:var(--accent);font-weight:600}
.blank{display:inline-block;min-width:4.5em;border-bottom:2px solid var(--accent);color:transparent}
.lab{font-size:12px;font-weight:600;color:var(--fg2);letter-spacing:.02em}
.chunk{background:var(--accent-soft);border-radius:5px;padding:1px 3px;box-decoration-break:clone;-webkit-box-decoration-break:clone}
.chunk.miss{background:color-mix(in srgb,#ff9f0a 16%,transparent)}
.chunk.hit{background:color-mix(in srgb,var(--ok) 14%,transparent)}
.ctx{position:fixed;z-index:60;min-width:180px;padding:5px;border-radius:12px;font-size:14px;display:none;
background:color-mix(in srgb,var(--card) 84%,transparent);backdrop-filter:saturate(180%) blur(30px);-webkit-backdrop-filter:saturate(180%) blur(30px);
box-shadow:0 0 0 .5px rgba(0,0,0,.14),0 10px 32px rgba(0,0,0,.18)}
.ctx button{display:flex;width:100%;gap:8px;align-items:baseline;padding:5px 10px;border-radius:7px;text-align:left;white-space:nowrap}
.ctx button b{font-weight:600}
.ctx button:hover{background:var(--accent);color:#fff}
.ctx hr{border:0;height:1px;background:var(--line);margin:4px 8px}
.toast{position:fixed;z-index:61;left:50%;bottom:32px;max-width:min(560px,calc(100vw - 32px));padding:10px 18px;border-radius:14px;
font-size:14px;background:var(--card);box-shadow:0 0 0 .5px rgba(0,0,0,.1),0 10px 32px rgba(0,0,0,.16);
opacity:0;transform:translate(-50%,8px);transition:opacity .2s,transform .25s cubic-bezier(.22,1,.36,1);pointer-events:none}
.toast.on{opacity:1;transform:translate(-50%,0)}
.toast b{font-weight:600}
.toast span{color:var(--fg2)}
"""

SPEAK_SVG = ('<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3z"/>'
             '<path d="M16.5 12a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z"/>'
             '<path d="M14 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6z"/></svg>')

COMMON_JS = """
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let _gb=null;
function gbVoice(){if(_gb)return _gb;const vs=speechSynthesis.getVoices();
  _gb=vs.find(v=>/Daniel|Kate|Serena|Oliver/.test(v.name)&&/en-GB/.test(v.lang))||vs.find(v=>/en-GB/.test(v.lang))||null;return _gb}
try{speechSynthesis.onvoiceschanged=()=>{_gb=null;gbVoice()}}catch(e){}
function speak(t){try{speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(t);u.lang='en-GB';u.rate=.92;const v=gbVoice();if(v)u.voice=v;speechSynthesis.speak(u)}catch(e){}}
function wordRe(c){const ws=[c.word,...(c.forms||[])].filter(Boolean).sort((a,b)=>b.length-a.length)
  .map(w=>w.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&'));
  return new RegExp('\\\\b('+ws.join('|')+')([a-z]{0,3})\\\\b','gi')}
function mark(c,s){return esc(s).replace(wordRe(c),'<mark>$1$2</mark>')}
// 右键英文单词：加入生词本。选中一段（最多 6 个词）就加这一段
let _ctx=null,_toastT=0;
function toast(h){let t=document.querySelector('.toast');if(!t){t=document.createElement('div');t.className='toast';document.body.appendChild(t)}
  t.innerHTML=h;t.classList.add('on');clearTimeout(_toastT);_toastT=setTimeout(()=>t.classList.remove('on'),2600)}
function wordAt(ev){
  const sel=String(getSelection()||'').trim();
  if(sel&&/^[A-Za-z][A-Za-z' -]*$/.test(sel)&&sel.split(/ +/).length<=6)return sel;
  const r=document.caretRangeFromPoint&&document.caretRangeFromPoint(ev.clientX,ev.clientY);
  if(!r||r.startContainer.nodeType!==3)return '';
  const t=r.startContainer.data,ok=ch=>/[A-Za-z'-]/.test(ch||'');let a=r.startOffset,b=a;
  while(a>0&&ok(t[a-1]))a--;while(b<t.length&&ok(t[b]))b++;
  return t.slice(a,b).replace(/^['-]+|['-]+$/g,'')}
function hideCtx(){if(_ctx)_ctx.style.display='none'}
async function addWord(w){hideCtx();
  try{const r=await fetch('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:w})});
    if(!r.ok)throw new Error(await r.text());const d=await r.json();
    if(d.error)toast(esc(d.error));
    else if(d.pending)toast('正在查 <b>'+esc(w)+'</b><span> · 稍后出现在生词本</span>');
    else toast((d.known?'<b>'+esc(d.word)+'</b><span> 已在生词本 · 今天提前复习</span>':'已加入 <b>'+esc(d.word)+'</b><span> · '+esc(d.meaning)+'</span>'));
    if(typeof onWordAdded==='function')onWordAdded(d)}
  catch(e){toast('没加上：'+esc(String(e.message||e).slice(0,60)))}}
document.addEventListener('contextmenu',ev=>{
  if(ev.target.closest('textarea,input'))return;
  const w=wordAt(ev);if(!/[A-Za-z]{2,}/.test(w))return hideCtx();
  ev.preventDefault();
  if(!_ctx){_ctx=document.createElement('div');_ctx.className='ctx';document.body.appendChild(_ctx)}
  _ctx.innerHTML='<button data-act="add">加入生词本 <b></b></button><hr><button data-act="say">朗读</button>';
  _ctx.querySelector('b').textContent=w;
  _ctx.querySelector('[data-act=add]').onclick=()=>addWord(w);
  _ctx.querySelector('[data-act=say]').onclick=()=>{hideCtx();speak(w)};
  _ctx.style.display='block';
  const x=Math.min(ev.clientX,innerWidth-_ctx.offsetWidth-8),y=Math.min(ev.clientY,innerHeight-_ctx.offsetHeight-8);
  _ctx.style.left=x+'px';_ctx.style.top=y+'px'});
document.addEventListener('mousedown',ev=>{if(_ctx&&!_ctx.contains(ev.target))hideCtx()},true);
addEventListener('scroll',hideCtx,true);addEventListener('blur',hideCtx);
document.addEventListener('keydown',ev=>{if(ev.key==='Escape')hideCtx()},true);
// 把句子按词块切开：[[文字, 是否词块, 第几个词块], ...]
function segs(c,s){const low=(s||'').toLowerCase(),hits=[];
  (c.w_chunks||[]).forEach((ch,k)=>{const j=low.indexOf(ch.toLowerCase());if(j>=0)hits.push([j,ch.length,k])});
  hits.sort((a,b)=>a[0]-b[0]);const out=[];let pos=0;
  hits.forEach(([j,n,k])=>{if(j<pos)return;if(j>pos)out.push([s.slice(pos,j),false,-1]);out.push([s.slice(j,j+n),true,k]);pos=j+n});
  if(pos<(s||'').length)out.push([s.slice(pos),false,-1]);return out}
function chunked(c,s,cls){return segs(c,s).map(([t,ch,k])=>ch?'<span class="chunk '+(cls?cls(k):'')+'">'+mark(c,t)+'</span>':mark(c,t)).join('')}
function norm(s){return (s||'').toLowerCase().replace(/[’‘]/g,"'").replace(/[^a-z' -]/g,' ').replace(/ +/g,' ').trim()}
function blank(c,s){let hit=false;const out=esc(s).replace(wordRe(c),()=>{hit=true;return '<span class="blank">____</span>'});return hit?out:null}
"""

REVIEW_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>今日复习 · 浮词</title>
<style>__CSS__
main{max-width:660px;margin:0 auto;padding:5vh 20px 140px}
.card{background:var(--card);border-radius:24px;box-shadow:var(--shadow);padding:40px 40px 34px;min-height:320px;
display:flex;flex-direction:column;transition:transform .25s cubic-bezier(.22,1,.36,1)}
.card.tap{cursor:pointer}
.kind{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;color:var(--fg3);letter-spacing:.04em;margin-bottom:20px}
.chip{font-size:11px;font-weight:600;color:var(--accent);background:var(--accent-soft);border-radius:6px;padding:2px 7px;letter-spacing:0}
.word{font-size:44px;line-height:1.1;font-weight:700;letter-spacing:-.02em}
.ph{color:var(--fg2);font-size:16px;margin:10px 0 0;display:flex;align-items:center;gap:10px}
.zh-big{font-size:26px;line-height:1.4;font-weight:600}
.sent{font-size:19px;line-height:1.6;margin-top:26px}
.sent-zh{color:var(--fg2);font-size:15px;margin-top:6px}
.task{font-size:18px;line-height:1.55;margin-top:6px}
.target{margin-top:22px;font-size:15px;color:var(--fg2)}
.target b{color:var(--fg);font-size:20px;font-weight:700;margin-right:6px}
textarea{width:100%;margin-top:16px;min-height:96px;resize:vertical;border:0;border-radius:14px;background:var(--fill);
padding:14px 16px;font:inherit;font-size:17px;line-height:1.5;color:var(--fg);outline:none}
textarea:focus{box-shadow:0 0 0 3px var(--accent-soft)}
.tools{display:flex;align-items:center;gap:10px;margin-top:12px}
.tools .sp{flex:1}
.btn{height:38px;padding:0 16px;border-radius:19px;font-size:14px;font-weight:600;display:inline-flex;align-items:center;gap:6px}
.btn.primary{background:var(--accent);color:#fff}.btn.primary:disabled{opacity:.45}
.btn.soft{background:var(--fill)}
.mic{width:44px;height:44px;border-radius:22px;background:var(--accent);color:#fff;display:inline-flex;align-items:center;justify-content:center}
.mic svg{width:20px;height:20px}
.mic.rec{background:var(--no);animation:pulse 1.2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,59,48,.45)}70%{box-shadow:0 0 0 12px rgba(255,59,48,0)}100%{box-shadow:0 0 0 0 rgba(255,59,48,0)}}
.small{font-size:13px;color:var(--fg3)}
.back{display:none;border-top:1px solid var(--line);margin-top:26px;padding-top:22px}
.flipped .back{display:block;animation:in .3s cubic-bezier(.22,1,.36,1)}
@keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.sense{margin-top:12px}.sense:first-child{margin-top:0}
.sense .zh{font-size:18px;margin-top:2px}
.row{margin-top:18px}.row:first-child{margin-top:0}.row p{margin-top:3px;font-size:15px}
.row p.en{font-size:17px}
.verdict{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;border-radius:8px;padding:3px 10px}
.verdict.ok{color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent)}
.verdict.fix{color:#ff9f0a;background:color-mix(in srgb,#ff9f0a 14%,transparent)}
.hint{text-align:center;color:var(--fg3);font-size:13px;margin-top:20px}
.actions{display:none;gap:14px;margin-top:22px}
.flipped~.actions{display:flex}
.flipped~.hint{display:none}
.actions button{flex:1;height:56px;border-radius:16px;font-size:17px;font-weight:600;background:var(--card);box-shadow:var(--shadow);display:flex;align-items:center;justify-content:center;gap:10px;transition:transform .15s}
.actions button:active{transform:scale(.97)}
.actions button.suggest{outline:2px solid currentColor;outline-offset:-2px}
#no{color:var(--no)}#yes{color:var(--ok)}
kbd{font:500 12px -apple-system;color:var(--fg3);border:1px solid var(--line);border-radius:5px;padding:1px 6px}
.done{text-align:center;padding-top:8vh;display:none}
.done h1{font-size:40px;font-weight:700;letter-spacing:-.02em}
.done p{color:var(--fg2);margin-top:12px;font-size:17px}
.done .btns{margin-top:32px;display:flex;gap:12px;justify-content:center}
.pill{height:44px;padding:0 22px;border-radius:22px;background:var(--accent);color:#fff;font-weight:600;font-size:15px;display:inline-flex;align-items:center}
.pill.ghost{background:var(--fill);color:var(--fg)}
.slang{max-width:660px;margin:36px auto 0;padding:0 20px;display:none}
.slang .in{border-top:1px solid var(--line);padding-top:16px;cursor:pointer}
.slang .t{display:flex;align-items:center;gap:10px;font-size:15px}
.slang .t b{font-weight:600}
.slang .t span{color:var(--fg2)}
.slang .more{display:none;font-size:14px;color:var(--fg2);margin-top:8px;line-height:1.6}
.slang.open .more{display:block}
.speak.sm{width:26px;height:26px}.speak.sm svg{width:13px;height:13px}
.ctxq{font-size:15px;line-height:1.5;color:var(--fg2)}
.cloze{line-height:2.15}
.gap{font:inherit;color:var(--fg);border:0;border-bottom:2px solid var(--accent);background:var(--accent-soft);border-radius:6px 6px 0 0;
padding:0 6px;margin:0 2px;outline:none;min-width:3em;max-width:100%}
.gap:focus{background:color-mix(in srgb,var(--accent) 18%,transparent)}
.gap::placeholder{color:var(--fg3);letter-spacing:.06em}
.gap.right{border-color:var(--ok);color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent)}
.gap.wrong{border-color:var(--no);color:var(--no);text-decoration:line-through;background:color-mix(in srgb,var(--no) 8%,transparent)}
.fix-ans{color:var(--ok);font-weight:600;margin:0 4px}
.chip.warn{color:#c93400;background:color-mix(in srgb,#ff9f0a 16%,transparent)}
.passage{font-size:18px;line-height:1.7}
.gq{margin-top:20px;font-size:15px;color:var(--fg2)}
.opt{display:flex;gap:12px;align-items:baseline;width:100%;text-align:left;padding:11px 14px;border-radius:12px;background:var(--fill);margin-top:8px;font-size:16px;line-height:1.4}
.opt span{font-weight:600;color:var(--fg3);min-width:1em}
.opt:hover{background:color-mix(in srgb,var(--accent) 12%,var(--fill))}
.opt.right{background:color-mix(in srgb,var(--ok) 16%,transparent);color:var(--ok)}
.opt.wrong{background:color-mix(in srgb,var(--no) 12%,transparent);color:var(--no)}
.opt.right span,.opt.wrong span{color:inherit}
.sum{font-size:15px;color:var(--fg2);margin-top:8px}
</style></head><body>
<div class="top"><span class="title">今日复习</span><div class="progress"><i id="bar"></i></div><span class="meta" id="cnt"></span><span class="sp"></span><a href="/weekly">本周练习</a><a href="/book">生词本</a></div>
<main>
  <div id="stage">
    <section class="card" id="card"></section>
    <div class="hint" id="hint"></div>
    <div class="actions"><button id="no">忘了 <kbd>←</kbd></button><button id="yes">记得 <kbd>→</kbd></button></div>
  </div>
  <section class="done" id="done"></section>
</main>
<section class="slang" id="slang"></section>
<script>__JS__
let cards=[],i=0,flipped=false,total=0,okN=0,noN=0,retried=new Set(),rec=null;
const $=id=>document.getElementById(id);
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
const MIC='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3z"/><path d="M17 11a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/></svg>';
function spk(t,cls){return '<button class="speak '+(cls||'')+'" data-say="'+esc(t)+'">__SPK__</button>'}
document.addEventListener('click',e=>{const b=e.target.closest('[data-say]');if(b){e.stopPropagation();speak(b.dataset.say)}},true);
async function load(){const r=await fetch('/api/queue');const d=await r.json();cards=d.cards;total=cards.length;i=0;okN=noN=0;retried=new Set();
  if(!cards.length){return finish(true)}$('stage').style.display='';$('done').style.display='none';show()}
function senses(c){return c.senses.map(s=>'<div class="sense"><div class="lab">'+esc(s.pos)+'</div><div class="zh">'+esc(s.zh)+'</div></div>').join('')}
function last(c){
  // 上次做这张卡的批改记录（本次提交前的那一条），用来对照同一个错有没有再犯
  const m=(c.mine||[]).filter(x=>x.kind===c._m).slice(-1)[0];if(!m)return '';
  const t=m.t||'',d=t.length>=10?(+t.slice(5,7))+'月'+(+t.slice(8,10))+'日':'';
  let b='<div class="row"><div class="lab">上次'+(d?' · '+d:'')+'</div>';
  if(m.orig)b+='<p class="sent-zh"><s>'+esc(m.orig)+'</s></p>';
  b+='<p class="en">'+mark(c,m.text)+' '+spk(m.text,'sm')+'</p>';
  if(m.why)b+='<p class="sent-zh">'+esc(m.why)+'</p>';
  return b+'</div>'}
function extra(c){let b='';
  if(c.use)b+='<div class="row"><div class="lab">考试用法</div><p>'+esc(c.use)+'</p></div>';
  if(c.note)b+='<div class="row"><div class="lab">记忆</div><p>'+esc(c.note)+'</p></div>';return b}
function show(){flipped=false;stopMic();const c=cards[i];const el=$('card');el.className='card';
  $('bar').style.width=(100*Math.min(i,total)/Math.max(total,1))+'%';$('cnt').textContent=Math.min(i+1,total)+' / '+total;
  const topic=(c.topic?'<span class="chip">'+esc(c.topic)+'</span>':'')+(c.extra?'<span class="chip" style="color:var(--fg2);background:var(--fill)">今日练习</span>':'');
  let mode=c.mode;if(mode==='produce'&&!(c.ex_en&&blank(c,c.ex_en)))mode='recall';
  if(mode==='cloze'&&!(c.w_model&&(c.w_chunks||[]).length))mode='produce';
  if(mode==='translate'&&!(c.w_model&&c.w_zh))mode='produce';
  if(mode==='produce'&&!(c.ex_en&&blank(c,c.ex_en)))mode='recall';c._m=mode;
  let h='';
  if(mode==='write'||mode==='speak'){
    const w=mode==='write';
    h='<div class="kind">'+(w?'写作 · 雅思大作文':'口语 · 雅思口语')+topic+'</div>'+
      '<div class="task">'+esc(w?c.w_q:c.s_q)+'</div>'+
      '<div class="target"><b>'+esc(c.word)+'</b>'+esc(c.meaning)+'</div>'+
      '<textarea id="ans" placeholder="'+(w?'用这个词写一句话':'点麦克风用这个词回答，或直接打字')+'"></textarea>'+
      '<div class="tools">'+(!w&&SR?'<button class="mic" id="mic" title="点击开始/停止">'+MIC+'</button>':'')+
      '<span class="small" id="tip">'+(!w&&!SR?'当前浏览器不支持语音识别，可直接打字':'⌘↩ 提交')+'</span><span class="sp"></span>'+
      '<button class="btn soft" id="skip">看范句</button><button class="btn primary" id="send">提交批改</button></div>'+
      '<div class="back" id="fb"></div>';
    $('hint').textContent=w?'写完提交，AI 按雅思写作标准批改':'说完提交，AI 按雅思口语标准批改，再跟读一遍';
  }else if(mode==='fix'){
    h='<div class="kind">改错<span class="chip warn">'+esc(c.tag)+'</span>'+(c.word?'<span class="chip">'+esc(c.word)+'</span>':'')+'</div>'+
      '<div class="ctxq">这是你之前写的句子，有一处'+esc(c.tag)+'问题。直接在下面改。</div>'+
      '<textarea id="ans" spellcheck="false">'+esc(c.orig)+'</textarea>'+
      '<div class="tools"><span class="small" id="tip">⌘↩ 核对</span><span class="sp"></span>'+
      '<button class="btn soft" id="skip">看答案</button><button class="btn primary" id="send">核对</button></div>'+
      '<div class="back" id="fb"></div>';
    $('hint').textContent='改掉自己犯过的错，比看例句更记得住';
  }else if(mode==='gmat'){
    h='<div class="kind">GMAT · 熟词僻义</div><div class="passage">'+mark(c,c.passage)+'</div>'+
      '<div class="gq">'+esc(c.q)+'</div><div id="opts">'+c.options.map((o,k)=>'<button class="opt" data-k="'+k+'"><span>'+'ABCD'[k]+'</span>'+esc(o)+'</button>').join('')+'</div>'+
      '<div class="back" id="fb"></div>';
    $('hint').textContent='按 A–D 选择';
  }else if(mode==='cloze'){
    h='<div class="kind">范句填空 · 雅思大作文'+topic+'</div>'+
      '<div class="ctxq">'+esc(c.w_q)+'</div>'+
      '<div class="sent cloze">'+segs(c,c.w_model).map(([t,ch,k])=>ch?
        '<input class="gap" data-ans="'+esc(t)+'" placeholder="'+esc(t.split(' ').map(x=>x[0]+'…').join(' '))+'" style="width:'+(t.length*.56+1.4).toFixed(1)+'em" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">':esc(t)).join('')+'</div>'+
      (c.w_zh?'<div class="sent-zh">'+esc(c.w_zh)+'</div>':'')+
      '<div class="tools"><span class="small" id="tip">首字母提示 · Tab 下一个空 · ↩ 核对</span><span class="sp"></span>'+
      '<button class="btn soft" id="skip">看答案</button><button class="btn primary" id="send">核对</button></div>'+
      '<div class="back" id="fb"></div>';
    $('hint').textContent='填出范句里的词块，背下整个搭配';
  }else if(mode==='translate'){
    h='<div class="kind">回译 · 雅思大作文'+topic+'</div>'+
      '<div class="ctxq">'+esc(c.w_q)+'</div>'+
      '<div class="task" style="margin-top:18px;font-size:20px">'+esc(c.w_zh)+'</div>'+
      '<div class="target"><b>'+esc(c.word)+'</b>'+esc(c.meaning)+'</div>'+
      '<textarea id="ans" placeholder="把这句中文写成英文，用上 '+esc(c.word)+'"></textarea>'+
      '<div class="tools"><span class="small" id="tip">⌘↩ 和范句对照</span><span class="sp"></span>'+
      '<button class="btn soft" id="skip">看范句</button><button class="btn primary" id="send">对照</button></div>'+
      '<div class="back" id="fb"></div>';
    $('hint').textContent='凭记忆写回整句，再对照范句里的词块';
  }else if(mode==='produce'){
    el.classList.add('tap');
    h='<div class="kind">想出这个英文单词'+topic+'</div><div class="zh-big">'+esc(c.senses.map(s=>s.zh).join('；'))+'</div>'+
      '<div class="sent">'+blank(c,c.ex_en)+'</div>'+
      '<div class="back"><div class="word" style="font-size:34px">'+esc(c.word)+'</div><div class="ph" style="margin-bottom:18px">'+esc(c.phonetic)+spk(c.word)+'</div>'+
      extra(c)+'<div class="row"><div class="lab">例句</div><p>'+mark(c,c.ex_en)+'</p>'+(c.ex_zh?'<p class="sent-zh">'+esc(c.ex_zh)+'</p>':'')+'</div></div>';
    $('hint').textContent='空格或点击卡片 翻面　·　S 发音';
  }else{
    el.classList.add('tap');
    h='<div class="kind">回想中文意思'+topic+'</div><div class="word">'+esc(c.word)+'</div><div class="ph">'+esc(c.phonetic)+spk(c.word)+'</div>'+
      (c.ex_en?'<div class="sent">'+mark(c,c.ex_en)+'</div>':'')+
      '<div class="back">'+senses(c)+extra(c)+(c.ex_zh?'<div class="row"><div class="lab">例句译文</div><p class="sent-zh">'+esc(c.ex_zh)+'</p></div>':'')+'</div>';
    $('hint').textContent='空格或点击卡片 翻面　·　S 发音';
  }
  el.innerHTML=h;
  document.querySelectorAll('.actions button').forEach(b=>b.classList.remove('suggest'));
  if(mode==='write'||mode==='speak'){
    const ta=$('ans');setTimeout(()=>ta.focus(),50);
    ta.onkeydown=e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();check()}};
    $('send').onclick=check;$('skip').onclick=()=>feedback(null);
    if($('mic'))$('mic').onclick=toggleMic;
  }else if(mode==='cloze'){
    const gaps=[...document.querySelectorAll('.gap')];setTimeout(()=>gaps[0]&&gaps[0].focus(),50);
    gaps.forEach(g=>g.onkeydown=e=>{if(e.key==='Enter'&&!e.isComposing){e.preventDefault();checkCloze()}});
    $('send').onclick=checkCloze;$('skip').onclick=checkCloze;
  }else if(mode==='fix'){
    const ta=$('ans');setTimeout(()=>{ta.focus();ta.setSelectionRange(ta.value.length,ta.value.length)},50);
    ta.onkeydown=e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();checkFix()}};
    $('send').onclick=checkFix;$('skip').onclick=()=>{ta.value='';checkFix()};
  }else if(mode==='gmat'){
    document.querySelectorAll('.opt').forEach(b=>b.onclick=()=>chooseGmat(+b.dataset.k));
  }else if(mode==='translate'){
    const ta=$('ans');setTimeout(()=>ta.focus(),50);
    ta.onkeydown=e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();checkTranslate()}};
    $('send').onclick=checkTranslate;$('skip').onclick=()=>{ta.value='';checkTranslate()};
  }
}
function diffMark(a,b){const have=new Set(norm(a).split(' '));return b.split(' ').map(w=>!norm(w)||have.has(norm(w))?esc(w):'<mark>'+esc(w)+'</mark>').join(' ')}
function checkFix(){if(flipped)return;const c=cards[i];const t=$('ans').value.trim();const ok=!!t&&norm(t)===norm(c.corrected);
  $('fb').innerHTML='<div class="row"><span class="verdict '+(ok?'ok':'fix')+'">'+(ok?'✓ 改对了':(t&&norm(t)!==norm(c.orig)?'和参考写法不同，对照一下':'还没改对'))+'</span></div>'+
    '<div class="row"><div class="lab">正确写法</div><p class="en">'+diffMark(c.orig,c.corrected)+' '+spk(c.corrected,'sm')+'</p></div>'+
    '<div class="row"><div class="lab">原句</div><p class="sent-zh"><s>'+esc(c.orig)+'</s></p></div>'+
    (c.why?'<div class="row"><div class="lab">为什么</div><p>'+esc(c.why)+'</p></div>':'');
  flipped=true;$('card').classList.add('flipped');$('send').disabled=true;$('send').textContent='已核对';$('skip').style.display='none';
  document.activeElement&&document.activeElement.blur();$(ok?'yes':'no').classList.add('suggest')}
function chooseGmat(k){if(flipped)return;const c=cards[i];const ok=k===c.answer;
  document.querySelectorAll('.opt').forEach(b=>{const j=+b.dataset.k;b.disabled=true;if(j===c.answer)b.classList.add('right');else if(j===k)b.classList.add('wrong')});
  $('fb').innerHTML='<div class="row"><span class="verdict '+(ok?'ok':'fix')+'">'+(ok?'✓ 选对了':'选错了')+'</span></div>'+
    '<div class="row"><div class="lab">这里的意思</div><p><b>'+esc(c.word)+'</b>　'+esc(c.sense)+'</p></div>'+
    (c.explain?'<div class="row"><div class="lab">解析</div><p>'+esc(c.explain)+'</p></div>':'');
  flipped=true;$('card').classList.add('flipped');$(ok?'yes':'no').classList.add('suggest')}
function modelRow(c,cls){return '<div class="row"><div class="lab">范句</div><p class="en">'+chunked(c,c.w_model,cls)+' '+spk(c.w_model,'sm')+'</p>'+
  (c.w_zh?'<p class="sent-zh">'+esc(c.w_zh)+'</p>':'')+'</div>'}
function reveal(h,ok){$('fb').innerHTML=h;flipped=true;$('card').classList.add('flipped');
  $('send').disabled=true;$('skip').style.display='none';document.activeElement&&document.activeElement.blur();
  $(ok?'yes':'no').classList.add('suggest');speak(cards[i].w_model)}
function checkCloze(){if(flipped)return;const c=cards[i];const gaps=[...document.querySelectorAll('.gap')];let right=0;
  gaps.forEach(g=>{const ok=!!g.value.trim()&&norm(g.value)===norm(g.dataset.ans);g.readOnly=true;g.classList.add(ok?'right':'wrong');
    if(ok)right++;else{if(!g.value.trim())g.style.display='none';const a=document.createElement('span');a.className='fix-ans';a.textContent=g.dataset.ans;g.after(a)}});
  $('send').textContent=right+' / '+gaps.length;
  reveal('<div class="row"><span class="verdict '+(right===gaps.length?'ok':'fix')+'">'+(right===gaps.length?'✓ 全部填对':'填对 '+right+' / '+gaps.length)+'</span></div>'+
    modelRow(c)+last(c)+extra(c),right===gaps.length)}
function checkTranslate(){if(flipped)return;const c=cards[i];const t=$('ans').value.trim();const mine=norm(t);
  const ch=c.w_chunks||[],hit=ch.map(x=>!!mine&&mine.includes(norm(x))),n=hit.filter(Boolean).length;
  let h='';
  if(t)h+='<div class="row"><span class="verdict '+(n===ch.length?'ok':'fix')+'">'+(n===ch.length?'✓ 词块都用上了':'词块 '+n+' / '+ch.length)+'</span></div>';
  h+=modelRow(c,k=>hit[k]?'hit':'miss');
  if(t)h+='<div class="row" id="ai"><button class="btn soft" id="aibtn">AI 批改我的句子</button></div>';
  reveal(h+last(c)+extra(c),!!t&&n===ch.length);
  $('send').textContent='已对照';
  if($('aibtn'))$('aibtn').onclick=()=>aiCheck(t)}
async function aiCheck(t){const c=cards[i];const b=$('aibtn');b.disabled=true;b.textContent='批改中…';
  try{const r=await fetch('/api/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:c.key,kind:'write',text:t})});
    if(!r.ok)throw new Error(await r.text());$('ai').outerHTML=aiRows(c,await r.json(),t,true)}
  catch(e){b.disabled=false;b.textContent='批改失败，再试一次'}}
function aiRows(c,res,mine,w){let h='';
  h+='<div class="row"><span class="verdict '+(res.ok?'ok':'fix')+'">'+(res.ok?'✓ 用得对':'需要修改')+'</span></div>';
  if(res.corrected&&res.corrected.trim()!==mine.trim())h+='<div class="row"><div class="lab">修改后</div><p class="en">'+mark(c,res.corrected)+'</p></div>';
  if(res.better)h+='<div class="row"><div class="lab">'+(w?'7 分版本':'更地道的说法')+'</div><p class="en">'+mark(c,res.better)+' '+spk(res.better,'sm')+'</p></div>';
  if(res.why)h+='<div class="row"><div class="lab">点评</div><p>'+esc(res.why)+'</p></div>';
  return h}
function stopMic(){if(rec){try{rec.stop()}catch(e){}rec=null}}
function toggleMic(){const b=$('mic');if(rec){stopMic();b.classList.remove('rec');return}
  const ta=$('ans');const base=ta.value?ta.value.trim()+' ':'';
  rec=new SR();rec.lang='en-GB';rec.interimResults=true;rec.continuous=true;
  rec.onresult=e=>{let t='';for(const r of e.results)t+=r[0].transcript;ta.value=base+t};
  rec.onerror=e=>{$('tip').textContent='语音识别不可用（'+e.error+'），可以直接打字'};
  rec.onend=()=>{rec=null;b.classList.remove('rec')};
  try{rec.start();b.classList.add('rec');$('tip').textContent='正在听… 再点一次结束'}catch(e){$('tip').textContent='语音识别启动失败，可以直接打字'}}
async function check(){const c=cards[i];const ta=$('ans');const t=ta.value.trim();if(!t){ta.focus();return}
  stopMic();const b=$('send');b.disabled=true;b.textContent='批改中…';
  try{const r=await fetch('/api/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:c.key,kind:c._m,text:t})});
    if(!r.ok)throw new Error(await r.text());feedback(await r.json(),t)}
  catch(e){b.disabled=false;b.textContent='提交批改';$('tip').textContent='批改失败：'+String(e.message||e).slice(0,60)}}
function feedback(res,mine){const c=cards[i];const w=c._m==='write';let h='';
  if(res)h+=aiRows(c,res,mine,w);
  const mq=w?c.w_model:c.s_model,mz=w?c.w_zh:c.s_zh;
  if(mq)h+='<div class="row"><div class="lab">'+(w?'范句':'参考回答')+'</div><p class="en">'+(w?chunked(c,mq):mark(c,mq))+' '+spk(mq,'sm')+'</p>'+(mz?'<p class="sent-zh">'+esc(mz)+'</p>':'')+'</div>';
  h+=last(c);
  h+=extra(c);
  $('fb').innerHTML=h;flipped=true;$('card').classList.add('flipped');
  $('send').disabled=true;$('send').textContent=res?'已批改':'提交批改';$('skip').style.display='none';
  document.activeElement&&document.activeElement.blur();
  if(res)$(res.ok?'yes':'no').classList.add('suggest');
  if(!w&&res&&res.better)speak(res.better);
}
function flip(){if(flipped)return;const c=cards[i];if(['write','speak','cloze','translate','fix','gmat'].includes(c._m))return;
  flipped=true;$('card').classList.add('flipped');if(c._m!=='produce')speak(c.word)}
async function grade(ok){if(!flipped)return;const c=cards[i];ok?okN++:noN++;
  fetch('/api/grade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:c.key,ok,kind:c._m,extra:!!c.extra})});
  if(!ok&&!retried.has(c.key)&&!['fix','gmat'].includes(c._m)){retried.add(c.key);cards.push(Object.assign({},c,{mode:'recall'}))}
  i++;if(i>=cards.length)return finish(false);show()}
async function finish(empty){stopMic();$('bar').style.width='100%';$('stage').style.display='none';const d=await (await fetch('/api/stats')).json();
  const el=$('done');el.style.display='block';
  const wk=d.weekly||{},wl=['write','speak'].filter(k=>wk[k]==null).length,top=d.errors_top||[];
  const more=(top.length?'<p class="sum">常犯错误 · 近 30 天　'+top.map(x=>esc(x.tag)+' '+x.n).join(' · ')+'</p>':'')+
    '<p class="sum">'+(wl?'本周练习还差 '+wl+' 项':'本周练习已完成 · 写作 '+wk.write+' · 口语 '+wk.speak)+'</p>';
  const weekBtn=wl?'<a class="pill" href="/weekly">本周练习</a>':'';
  if(empty){el.innerHTML='<h1>今天的复习都完成了</h1><p>明天 '+d.tomorrow+' 个 · 已掌握 '+d.mastered+' / '+d.total+'</p>'+more+'<div class="btns">'+weekBtn+'<a class="pill ghost" href="/book">打开生词本</a></div>';return}
  el.innerHTML='<h1>完成 '+total+' 个</h1><p>记得 '+okN+' · 忘了 '+noN+'</p><p>'+(d.due?'还有 '+d.due+' 个到期':'明天 '+d.tomorrow+' 个')+' · 已掌握 '+d.mastered+' / '+d.total+'</p>'+more+
   '<div class="btns">'+(d.due?'<button class="pill" onclick="load()">再来一组</button>':weekBtn)+'<a class="pill ghost" href="/book">打开生词本</a></div>'}
async function slang(){try{const r=await fetch('/api/slang');if(!r.ok)return;const s=await r.json();if(!s||!s.phrase)return;
  const el=$('slang');el.style.display='block';
  el.innerHTML='<div class="in"><div class="lab" style="margin-bottom:6px">每日 slang</div><div class="t"><b>'+esc(s.phrase)+'</b><span>'+esc(s.meaning)+'</span>'+spk(s.phrase,'sm')+'</div>'+
   '<div class="more">'+esc(s.example)+' '+spk(s.example,'sm')+'<br>'+esc(s.example_zh)+(s.note?'<br>'+esc(s.note):'')+'</div></div>';
  el.querySelector('.in').onclick=()=>el.classList.toggle('open')}catch(e){}}
$('card').onclick=e=>{if(!flipped&&$('card').classList.contains('tap'))flip()};
$('yes').onclick=()=>grade(true);$('no').onclick=()=>grade(false);
document.onkeydown=e=>{if(e.target.tagName==='TEXTAREA'||e.target.tagName==='INPUT')return;
  const cc=cards[i];if(cc&&cc._m==='gmat'&&!flipped&&!e.metaKey&&!e.ctrlKey){const k='abcd1234'.indexOf(e.key.toLowerCase());if(k>=0){chooseGmat(k%4);return}}
  if(e.key===' '){e.preventDefault();flip()}else if(e.key==='ArrowRight'||e.key==='2')grade(true);
  else if(e.key==='ArrowLeft'||e.key==='1')grade(false);else if(e.key==='s'||e.key==='S'){const c=cards[i];c&&speak(c.word)}};
load();slang();
</script></body></html>"""

BOOK_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>生词本 · 浮词</title>
<style>__CSS__
main{max-width:720px;margin:0 auto;padding:28px 20px 120px}
h1{font-size:34px;font-weight:700;letter-spacing:-.02em}
.sub{color:var(--fg2);margin-top:4px;font-size:15px}
.tools{display:flex;gap:12px;margin:22px 0 10px;flex-wrap:wrap}
.search{flex:1;min-width:200px;height:38px;border-radius:10px;border:0;background:var(--fill);padding:0 14px;font:inherit;font-size:15px;color:var(--fg);outline:none}
.search:focus{box-shadow:0 0 0 3px var(--accent-soft)}
.seg{display:inline-flex;padding:2px;border-radius:9px;background:var(--fill);height:38px}
.seg button{padding:0 14px;border-radius:7px;font-size:13px;font-weight:500}
.seg button.on{background:var(--card);box-shadow:0 1px 3px rgba(0,0,0,.12)}
.topics{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
.topics button{height:28px;padding:0 12px;border-radius:14px;font-size:13px;background:var(--fill);color:var(--fg2)}
.topics button.on{background:var(--accent);color:#fff}
.topics button i{font-style:normal;opacity:.6;font-variant-numeric:tabular-nums;margin-left:2px}
.topics button.zero{color:var(--fg3)}
.topics button.zero.on{color:#fff}
.list{background:var(--card);border-radius:18px;box-shadow:var(--shadow);overflow:hidden}
.item{padding:14px 20px;border-bottom:1px solid var(--line);cursor:pointer}
.item:last-child{border-bottom:0}
.head{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 14px;align-items:center}
.w{font-size:18px;font-weight:600;letter-spacing:-.01em}
.w small{font-size:13px;font-weight:400;color:var(--fg2);margin-left:8px}
.m{grid-column:1;color:var(--fg2);font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.st{grid-row:1/3;grid-column:2;display:flex;align-items:center;gap:8px;font-size:13px;color:var(--fg2);font-variant-numeric:tabular-nums}
.chip{font-size:11px;font-weight:600;color:var(--accent);background:var(--accent-soft);border-radius:6px;padding:2px 7px}
.dot{width:8px;height:8px;border-radius:4px;background:var(--accent)}
.dot.mastered{background:var(--ok)}
.detail{display:none;padding-top:12px;cursor:auto}
.open .detail{display:block}
.detail .row{margin-top:12px}.detail p{font-size:15px;margin-top:2px}
.detail p.q{color:var(--fg2);font-size:13px}
.detail p.zh{color:var(--fg2);font-size:14px}
.speak.sm{width:26px;height:26px;margin-left:4px}.speak.sm svg{width:13px;height:13px}
.empty{padding:48px 20px;text-align:center;color:var(--fg2)}
.err-sum{color:var(--fg2);font-size:14px;margin:0 0 14px}
.err-sum b{color:var(--fg);font-weight:600}
.e-item{padding:14px 20px;border-bottom:1px solid var(--line)}
.e-item:last-child{border-bottom:0}
.e-head{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--fg2)}
.e-head .sp{flex:1}
.tag{font-size:11px;font-weight:600;color:#c93400;background:color-mix(in srgb,#ff9f0a 16%,transparent);border-radius:6px;padding:2px 7px}
.e-item s{display:block;color:var(--fg3);font-size:15px;margin-top:6px}
.e-item .c{font-size:16px;margin-top:2px}
.e-item .why{color:var(--fg2);font-size:14px;margin-top:2px}
.ok-mark{color:var(--ok);font-weight:600}
.go{height:34px;padding:0 16px;border-radius:17px;background:var(--accent);color:#fff;font-size:14px;font-weight:600;display:inline-flex;align-items:center}
</style></head><body>
<div class="top"><span class="sp"></span><a class="go" id="go" href="/review">开始复习</a></div>
<main>
  <h1>生词本</h1><p class="sub" id="sub"></p>
  <div class="tools"><input class="search" id="q" placeholder="搜索单词或中文" autofocus>
    <div class="seg" id="seg"><button data-f="all" class="on">全部</button><button data-f="learning">学习中</button><button data-f="mastered">已掌握</button><button data-f="errors">错误本</button></div></div>
  <div class="topics" id="topics"></div>
  <div class="list" id="list"></div>
</main>
<script>__JS__
let words=[],filter='all',topic='',errs=null;const $=id=>document.getElementById(id);
function spk(t){return '<button class="speak sm" data-say="'+esc(t)+'">__SPK__</button>'}
document.addEventListener('click',e=>{const b=e.target.closest('[data-say]');if(b){e.stopPropagation();speak(b.dataset.say)}},true);
function onWordAdded(){load()}
async function load(){const d=await (await fetch('/api/words')).json();words=d.words;const s=d.stats;
  $('sub').textContent='共 '+s.total+' 个 · 学习中 '+s.learning+' · 已掌握 '+s.mastered;
  const n=s.due+(s.practice||0);$('go').textContent=n?'开始复习 · '+n:'今日已完成';
  const tc=s.topics||{};const none=words.filter(w=>!w.topic).length;
  $('topics').innerHTML=['<button data-t="" class="on">全部话题</button>'].concat(Object.keys(tc).map(t=>
    '<button data-t="'+esc(t)+'"'+(tc[t]?'':' class="zero"')+'>'+esc(t)+' <i>'+tc[t]+'</i></button>'))
    .concat(none?['<button data-t="__none">未分类 <i>'+none+'</i></button>']:[]).join('');
  render()}
function row(lab,en,zh,q){return '<div class="row"><div class="lab">'+lab+'</div>'+(q?'<p class="q">'+esc(q)+'</p>':'')+'<p>'+en+'</p>'+(zh?'<p class="zh">'+esc(zh)+'</p>':'')+'</div>'}
function renderErrors(){const q=$('q').value.trim().toLowerCase();$('topics').style.display='none';
  if(!errs){$('list').innerHTML='<div class="empty">加载中…</div>';return}
  const rows=errs.errors.filter(x=>!q||(x.orig+' '+x.corrected+' '+x.tag+' '+(x.word||'')).toLowerCase().includes(q));
  const top=errs.top||[];
  const sum=top.length?'<p class="err-sum">近 30 天最常犯　'+top.map(x=>'<b>'+esc(x.tag)+'</b> '+x.n).join(' · ')+'　·　待改 '+errs.open+' 条</p>':'';
  if(!rows.length){$('list').innerHTML=sum+'<div class="empty">'+(errs.errors.length?'没有匹配的错误':'还没有错误记录。写作卡、口语卡和每周练习批改出的错误会收在这里。')+'</div>';return}
  $('list').innerHTML='<div style="padding:16px 20px 0">'+sum+'</div>'+rows.map(x=>'<div class="e-item"><div class="e-head"><span class="tag">'+esc(x.tag)+'</span>'+
    (x.word?'<span>'+esc(x.word)+'</span>':'')+'<span class="sp"></span>'+(x.done?'<span class="ok-mark">✓ 已改对</span>':'<span>'+esc((x.t||'').slice(5,10))+'</span>')+'</div>'+
    '<s>'+esc(x.orig)+'</s><div class="c">'+esc(x.corrected)+spk(x.corrected)+'</div>'+(x.why?'<div class="why">'+esc(x.why)+'</div>':'')+'</div>').join('')}
function render(){if(filter==='errors')return renderErrors();$('topics').style.display='';const q=$('q').value.trim().toLowerCase();
  const rows=words.filter(w=>(filter==='all'||w.state===filter)&&(!topic||(topic==='__none'?!w.topic:w.topic===topic))&&(!q||w.word.toLowerCase().includes(q)||(w.meaning||'').includes(q)));
  if(!rows.length){$('list').innerHTML='<div class="empty">'+(words.length?(topic&&topic!=='__none'?'这个话题还没有生词':'没有匹配的词'):'还没有生词。鼠标停在悬浮球上，输入单词回车即可记录。')+'</div>';return}
  $('list').innerHTML=rows.map(w=>'<div class="item"><div class="head"><div class="w">'+esc(w.word)+'<small>'+esc(w.phonetic)+'</small></div>'+
   '<div class="st">'+(w.topic?'<span class="chip">'+esc(w.topic)+'</span>':'')+(w.count>1?w.count+'×':'')+'<span class="dot '+w.state+'" title="'+(w.state==='mastered'?'已掌握':'学习中')+'"></span></div>'+
   '<div class="m">'+esc(w.meaning)+'</div></div><div class="detail">'+
   (w.use?row('考试用法',esc(w.use)):'')+
   (w.w_model?row('写作范句',chunked(w,w.w_model)+spk(w.w_model),w.w_zh,w.w_q):'')+
   (w.s_model?row('口语表达',mark(w,w.s_model)+spk(w.s_model),w.s_zh,w.s_q):'')+
   ((w.mine||[]).length?row('我的句子',w.mine.map(m=>mark(w,m.text)+spk(m.text)).join('<br>')):'')+
   (w.note?row('记忆',esc(w.note)):'')+
   (!w.w_model&&w.ex_en?row('例句',mark(w,w.ex_en)+spk(w.ex_en),w.ex_zh):'')+
   '</div></div>').join('');
  document.querySelectorAll('.item').forEach(el=>el.onclick=e=>{if(!e.target.closest('.detail'))el.classList.toggle('open')})}
$('q').oninput=render;
$('seg').onclick=async e=>{const b=e.target.closest('button');if(!b)return;filter=b.dataset.f;document.querySelectorAll('#seg button').forEach(x=>x.classList.toggle('on',x===b));
  render();if(filter==='errors'){errs=await (await fetch('/api/errors')).json();render()}};
$('topics').onclick=e=>{const b=e.target.closest('button');if(!b)return;topic=b.dataset.t;document.querySelectorAll('#topics button').forEach(x=>x.classList.toggle('on',x===b));render()};
load();
</script></body></html>"""


WEEKLY_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>本周练习 · 浮词</title>
<style>__CSS__
main{max-width:720px;margin:0 auto;padding:28px 20px 120px}
h1{font-size:34px;font-weight:700;letter-spacing:-.02em}
.sub{color:var(--fg2);margin-top:4px;font-size:15px}
.seg{display:inline-flex;padding:2px;border-radius:9px;background:var(--fill);height:36px;margin:22px 0 14px}
.seg button{padding:0 16px;border-radius:7px;font-size:13px;font-weight:500}
.seg button.on{background:var(--card);box-shadow:0 1px 3px rgba(0,0,0,.12)}
.seg b{color:var(--ok);font-weight:600;margin-left:4px}
.panel{background:var(--card);border-radius:22px;box-shadow:var(--shadow);padding:32px 34px}
.q{font-size:18px;line-height:1.55;margin-top:8px}
.cue{margin-top:8px;font-size:18px;line-height:1.55}
.cue ul{margin:8px 0 0;padding-left:20px;color:var(--fg2);font-size:16px}
.items{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.it{border-radius:10px;background:var(--fill);padding:6px 10px;font-size:14px;line-height:1.35;transition:background .2s}
.it b{font-weight:600}
.it small{display:block;color:var(--fg2);font-size:12px}
.it.used{background:color-mix(in srgb,var(--ok) 14%,transparent)}
.it.used b{color:var(--ok)}
textarea{width:100%;margin-top:18px;min-height:220px;resize:vertical;border:0;border-radius:14px;background:var(--fill);
padding:14px 16px;font:inherit;font-size:17px;line-height:1.6;color:var(--fg);outline:none}
textarea:focus{box-shadow:0 0 0 3px var(--accent-soft)}
.tools{display:flex;align-items:center;gap:12px;margin-top:12px}
.tools .sp{flex:1}
.small{font-size:13px;color:var(--fg3);font-variant-numeric:tabular-nums}
.btn{height:38px;padding:0 18px;border-radius:19px;font-size:14px;font-weight:600;display:inline-flex;align-items:center}
.btn.primary{background:var(--accent);color:#fff}.btn.primary:disabled{opacity:.45}
.mic{width:44px;height:44px;border-radius:22px;background:var(--accent);color:#fff;display:inline-flex;align-items:center;justify-content:center}
.mic svg{width:20px;height:20px}
.mic.rec{background:var(--no);animation:pulse 1.2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,59,48,.45)}70%{box-shadow:0 0 0 12px rgba(255,59,48,0)}100%{box-shadow:0 0 0 0 rgba(255,59,48,0)}}
.timer{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--fg2)}
.timer.ok{color:var(--ok)}
.res{border-top:1px solid var(--line);margin-top:26px;padding-top:24px;display:none}
.res.on{display:block;animation:in .3s cubic-bezier(.22,1,.36,1)}
@keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.score{display:flex;align-items:flex-end;gap:28px;flex-wrap:wrap}
.big{font-size:56px;line-height:1;font-weight:700;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.big small{display:block;font-size:12px;font-weight:600;color:var(--fg2);letter-spacing:.02em;margin-top:6px}
.crit{display:flex;gap:22px;flex-wrap:wrap}
.crit div{font-size:12px;color:var(--fg2);font-weight:600}
.crit b{display:block;font-size:22px;color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}
.row{margin-top:22px}.row p{margin-top:4px;font-size:15px}
.row p.en{font-size:17px;line-height:1.6}
ol.tips{margin:6px 0 0;padding-left:20px;font-size:15px}
ol.tips li{margin-top:4px}
.err{margin-top:10px;font-size:15px}
.err s{color:var(--fg3)}
.err .c{margin-top:2px}
.err .why{color:var(--fg2);font-size:14px;margin-top:2px}
.tag{font-size:11px;font-weight:600;color:#c93400;background:color-mix(in srgb,#ff9f0a 16%,transparent);border-radius:6px;padding:2px 7px;margin-right:6px}
.note{color:var(--fg3);font-size:13px;margin-top:8px}
.hist{margin-top:28px}
.hist table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
.hist td,.hist th{padding:8px 4px;border-bottom:1px solid var(--line);text-align:left}
.hist th{color:var(--fg2);font-weight:600;font-size:12px}
.msg{padding:56px 20px;text-align:center;color:var(--fg2)}
.speak.sm{width:26px;height:26px}.speak.sm svg{width:13px;height:13px}
</style></head><body>
<div class="top"><span class="title">本周练习</span><span class="meta" id="wk"></span><span class="sp"></span><a href="/review">今日复习</a><a href="/book">生词本</a></div>
<main>
  <h1>本周练习</h1><p class="sub">用这周的生词写一段、说一段，按雅思标准估分。每周一次。</p>
  <div id="body"><div class="msg">正在根据本周生词出题…</div></div>
</main>
<script>__JS__
const $=id=>document.getElementById(id);
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
const MIC='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3z"/><path d="M17 11a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/></svg>';
const CRIT={TR:'任务回应',CC:'连贯衔接',LR:'词汇',GRA:'语法',FC:'流利连贯'};
let D=null,tab='write',rec=null,t0=0,tick=0;
function spk(t){return '<button class="speak sm" data-say="'+esc(t)+'">__SPK__</button>'}
document.addEventListener('click',e=>{const b=e.target.closest('[data-say]');if(b){e.stopPropagation();speak(b.dataset.say)}},true);
async function load(){
  try{const r=await fetch('/api/weekly');if(!r.ok)throw new Error(await r.text());D=await r.json()}
  catch(e){$('body').innerHTML='<div class="msg">出题失败：'+esc(String(e.message||e).slice(0,80))+'<br><br><button class="btn primary" onclick="location.reload()">再试一次</button></div>';return}
  $('wk').textContent=D.week.replace('-W',' 年第 ')+' 周';
  if(D.few||!D.task){$('body').innerHTML='<div class="msg">本周生词不到 3 个。先查几个词再来。</div>';return}
  render()}
function items(list){return list.map((x,k)=>'<div class="it" data-k="'+k+'"><b>'+esc(x.word)+'</b>'+(x.chunk?'<small>'+esc(x.chunk)+'</small>':'')+'</div>').join('')}
function render(){const T=D.task,w=tab==='write',done=D[tab];
  const segs='<div class="seg" id="seg"><button data-t="write"'+(w?' class="on"':'')+'>写作段落'+(D.write?'<b>'+fmt(D.write.result.overall)+'</b>':'')+'</button>'+
    '<button data-t="speak"'+(!w?' class="on"':'')+'>口语 Part 2'+(D.speak?'<b>'+fmt(D.speak.result.overall)+'</b>':'')+'</button></div>';
  let h='<div class="panel">';
  if(w)h+='<div class="lab">雅思大作文 · 写一个主体段（120–180 词）'+(T.topic?' · '+esc(T.topic):'')+'</div><div class="q">'+esc(T.write)+'</div>';
  else h+='<div class="lab">雅思口语 Part 2 · 准备 1 分钟，说 1–2 分钟</div><div class="cue">'+esc(T.card)+'<ul>'+(T.points||[]).map(p=>'<li>'+esc(p)+'</li>').join('')+'</ul></div>';
  h+='<div class="lab" style="margin-top:22px">尽量用上</div><div class="items" id="items">'+items(w?T.w_items:T.s_items)+'</div>';
  h+='<textarea id="ans" placeholder="'+(w?'观点句 → 解释 → 例子 → 小结':'点麦克风开始说，也可以直接打字')+'">'+esc(done?done.text:'')+'</textarea>';
  h+='<div class="tools">'+(!w&&SR?'<button class="mic" id="mic">'+MIC+'</button><span class="timer" id="timer">0:00</span>':'')+
    '<span class="small" id="cnt"></span><span class="sp"></span><span class="small" id="tip">⌘↩ 评分</span>'+
    '<button class="btn primary" id="send">'+(done?'重新评分':'评分')+'</button></div>';
  h+='<div class="res" id="res"></div></div>'+hist();
  $('body').innerHTML=segs+h;
  $('seg').onclick=e=>{const b=e.target.closest('button');if(!b||b.dataset.t===tab)return;stopMic();tab=b.dataset.t;render()};
  const ta=$('ans');ta.oninput=update;ta.onkeydown=e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();send()}};
  $('send').onclick=send;if($('mic'))$('mic').onclick=toggleMic;
  update();if(done)showRes(done.result,false);else setTimeout(()=>ta.focus(),50)}
function fmt(v){return v==null?'—':(+v).toFixed(1)}
function update(){const t=$('ans').value,list=tab==='write'?D.task.w_items:D.task.s_items;
  const n=(t.match(/[A-Za-z'-]+/g)||[]).length;$('cnt').textContent=n+' 词';
  list.forEach((x,k)=>{const used=wordRe({word:x.word,forms:x.forms||[]}).test(t);document.querySelector('.it[data-k="'+k+'"]').classList.toggle('used',used)})}
async function send(){const t=$('ans').value.trim();if((t.match(/[A-Za-z'-]+/g)||[]).length<20){$('tip').textContent='至少写 20 个词';return}
  stopMic();const b=$('send');b.disabled=true;b.textContent='评分中…';$('tip').textContent='约 20–40 秒';
  try{const r=await fetch('/api/weekly/assess',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:tab,text:t})});
    if(!r.ok)throw new Error(await r.text());const res=await r.json();D[tab]={text:t,result:res};
    const v=await (await fetch('/api/weekly')).json();D.history=v.history;
    render();$('res').scrollIntoView({behavior:'smooth',block:'start'})}
  catch(e){b.disabled=false;b.textContent='评分';$('tip').textContent='评分失败：'+String(e.message||e).slice(0,50)}}
function showRes(r,anim){const w=tab==='write',keys=w?['TR','CC','LR','GRA']:['FC','LR','GRA'];
  const words=(w?D.task.w_items:D.task.s_items);
  let better=esc(r.better||'');words.forEach(x=>{better=better.replace(wordRe({word:x.word,forms:x.forms||[]}),'<mark>$1$2</mark>')});
  let h='<div class="score"><div class="big">'+fmt(r.overall)+'<small>预估分</small></div><div class="crit">'+
    keys.map(k=>'<div>'+CRIT[k]+'<b>'+fmt((r.bands||{})[k])+'</b></div>').join('')+'</div></div>';
  if(!w)h+='<p class="note">发音无法从文字判断，不计入。</p>';
  if((r.tips||[]).length)h+='<div class="row"><div class="lab">先改这几处</div><ol class="tips">'+r.tips.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ol></div>';
  if(r.better)h+='<div class="row"><div class="lab">'+(w?'7.5 分版本':'地道说法 · 跟读一遍')+'</div><p class="en">'+better+' '+spk(r.better)+'</p></div>';
  if((r.errors||[]).length)h+='<div class="row"><div class="lab">语言错误 · 已加入错误本</div>'+r.errors.map(e=>
    '<div class="err"><s>'+esc(e.orig)+'</s><div class="c"><span class="tag">'+esc(e.tag)+'</span>'+esc(e.corrected)+'</div>'+(e.why?'<div class="why">'+esc(e.why)+'</div>':'')+'</div>').join('')+'</div>';
  $('res').innerHTML=h;$('res').classList.add('on')}
function hist(){const rows=(D.history||[]).filter(x=>x.write!=null||x.speak!=null);if(!rows.length)return '';
  return '<div class="hist"><div class="lab">分数记录</div><table><tr><th>周</th><th>写作</th><th>口语</th></tr>'+
    rows.slice().reverse().map(x=>'<tr><td>'+esc(x.week)+'</td><td>'+fmt(x.write)+'</td><td>'+fmt(x.speak)+'</td></tr>').join('')+'</table></div>'}
function stopMic(){if(rec){try{rec.stop()}catch(e){}rec=null}clearInterval(tick)}
function toggleMic(){const b=$('mic');if(rec){stopMic();b.classList.remove('rec');return}
  const ta=$('ans');const base=ta.value?ta.value.trim()+' ':'';
  rec=new SR();rec.lang='en-GB';rec.interimResults=true;rec.continuous=true;
  rec.onresult=e=>{let t='';for(const r of e.results)t+=r[0].transcript;ta.value=base+t;update()};
  rec.onerror=e=>{$('tip').textContent='语音识别不可用（'+e.error+'），可以直接打字'};
  rec.onend=()=>{rec=null;b.classList.remove('rec');clearInterval(tick)};
  try{rec.start();b.classList.add('rec');t0=Date.now();clearInterval(tick);
    tick=setInterval(()=>{const s=Math.floor((Date.now()-t0)/1000),el=$('timer');if(!el)return;
      el.textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0');el.classList.toggle('ok',s>=60)},250);
    $('tip').textContent='正在听… 再点一次结束'}catch(e){$('tip').textContent='语音识别启动失败，可以直接打字'}}
load();
</script></body></html>"""


def _page(tpl):
    return (tpl.replace("__CSS__", APP_CSS).replace("__JS__", COMMON_JS).replace("__SPK__", SPEAK_SVG))


def start_server(core, call):
    """启动本机服务。call(fn, *args) 在主线程执行并返回结果。返回端口号。"""
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    review_html, book_html, weekly_html = _page(REVIEW_PAGE), _page(BOOK_PAGE), _page(WEEKLY_PAGE)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _local(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code, body, ctype):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj):
            self._send(200, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

        def do_GET(self):
            if not self._local():
                return self._send(403, "forbidden", "text/plain")
            path = self.path.split("?")[0]
            if path in ("/", "/review"):
                self._send(200, review_html, "text/html; charset=utf-8")
            elif path == "/book":
                self._send(200, book_html, "text/html; charset=utf-8")
            elif path == "/weekly":
                self._send(200, weekly_html, "text/html; charset=utf-8")
            elif path == "/api/errors":
                self._json(call(core.error_book))
            elif path == "/api/weekly":
                try:
                    prep = call(core.weekly_prepare)
                    if not prep["ready"] and not prep.get("few"):
                        task = gen_weekly(prep["cfg"], prep["topic"], prep["w_items"] + prep["s_items"])
                        call(core.weekly_set_task, prep["week"], task, prep)
                    v = call(core.weekly_view)
                    v["few"] = bool(prep.get("few"))
                    self._json(v)
                except Exception as ex:
                    self._send(503, str(ex), "text/plain; charset=utf-8")
            elif path == "/api/queue":
                self._json(call(core.queue))
            elif path == "/api/words":
                self._json(call(core.book))
            elif path == "/api/stats":
                self._json(call(core.stats))
            elif path == "/api/slang":
                try:
                    self._json(core.slang_today())
                except Exception as ex:
                    self._send(503, str(ex), "text/plain; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if not self._local():
                return self._send(403, "forbidden", "text/plain")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception as ex:
                return self._send(400, str(ex), "text/plain; charset=utf-8")
            try:
                if self.path == "/api/grade":
                    return self._json(call(core.grade, str(body["key"]), bool(body["ok"]),
                                           str(body.get("kind") or ""), bool(body.get("extra"))))
                if self.path == "/api/weekly/assess":
                    kind, text = str(body.get("kind")), str(body.get("text") or "").strip()[:4000]
                    if kind not in ("write", "speak") or len(text.split()) < 20:
                        return self._send(400, "至少写 20 个词", "text/plain; charset=utf-8")
                    prep = call(core.weekly_assess_prepare, kind)
                    res = assess(prep["cfg"], kind, prep["q"], prep["items"], text)
                    call(core.weekly_save, prep["week"], kind, text, res)
                    return self._json(res)
                if self.path == "/api/add":
                    return self._json(call(core.add_word, str(body.get("text") or "")[:80]))
                if self.path == "/api/check":
                    key, kind, text = str(body["key"]), str(body["kind"]), str(body["text"]).strip()[:600]
                    if kind not in ("write", "speak") or not text:
                        return self._send(400, "bad request", "text/plain; charset=utf-8")
                    prep = call(core.check_prepare, key, kind)
                    res = check_sentence(prep["cfg"], kind, prep["w"], prep["m"], prep["q"], text)
                    keep = res["corrected"] or (text if res["ok"] else "")
                    if keep:
                        call(core.save_mine, key, kind, prep["q"], keep, text,
                             res.get("better", ""), res.get("why", ""), res.get("ok", False))
                    if res["corrected"] and res["tag"]:
                        call(core.add_errors, [{"orig": text, "corrected": res["corrected"], "tag": res["tag"],
                                                "why": res["why"]}], key, prep["w"], kind)
                    return self._json(res)
                return self._send(404, "not found", "text/plain")
            except Exception as ex:
                self._send(500, str(ex), "text/plain; charset=utf-8")

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", SERVER_PORT), Handler)
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


# ───────────────────────── macOS 悬浮球 ─────────────────────────
#
# 一块玻璃，三种形态：
#   球  48×48           待命
#   条  W×56            鼠标停留后展开，可输入
#   条+结果  W×(56+n)    回车后同一块玻璃向下长出释义
# 尺寸、字号参照 macOS 26 Spotlight；材质优先用系统 Liquid Glass，旧系统退回毛玻璃。

BALL = 48
BAR_H = 56
BAR_W = 400
PAD = 22
HOVER_DELAY = 0.15   # 鼠标停留多久才展开（防误触）
LEAVE_DELAY = 0.6    # 鼠标离开多久后收起
PASSIVE_HOLD = 4.0   # 复制即记录：结果自动停留几秒
EDGE_MARGIN = 10     # 球吸附到屏幕边缘时留的距离
GLASS_CLEAR = True   # 清透玻璃（False 为系统常规磨砂玻璃）
GLASS_TINT = 0.0     # 系统玻璃自带的着色（对小尺寸几乎无效，用下面的烟色层代替）
THEME = "light"      # light：白玻璃深色字；dark：烟色玻璃白字
SMOKE = 0.58         # 玻璃里垫的那层白（或深色）的浓度：越高越白、越不透（0–1）

POS_ZH = {
    "n.": "名词", "v.": "动词", "vt.": "及物动词", "vi.": "不及物动词", "adj.": "形容词",
    "adv.": "副词", "prep.": "介词", "conj.": "连词", "pron.": "代词", "int.": "感叹词",
    "interj.": "感叹词", "phr.": "短语", "num.": "数词", "art.": "冠词", "aux.": "助动词",
    "abbr.": "缩写", "idiom": "习语",
}


def pos_label(p):
    p = (p or "").strip()
    return POS_ZH.get(p.lower(), p)


def run_mac():
    import objc
    from Foundation import NSObject, NSMakeRect, NSTimer, NSBundle
    from AppKit import (
        NSApplication, NSPanel, NSView, NSVisualEffectView, NSTextField, NSTextView, NSFont,
        NSColor, NSImage, NSImageView, NSScreen, NSMenu, NSMenuItem, NSPasteboard,
        NSPasteboardTypeString, NSAnimationContext, NSAlert, NSEvent, NSProgressIndicator,
        NSAttributedString, NSMutableAttributedString, NSFontAttributeName,
        NSForegroundColorAttributeName, NSParagraphStyleAttributeName, NSMutableParagraphStyle,
        NSKernAttributeName, NSBezierPath, NSWorkspace, NSURL, NSFontManager, NSBox, NSButton,
    )
    from PyObjCTools import AppHelper

    # 进程名显示为「浮词」而不是 Python
    try:
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = APP_NAME
    except Exception:
        pass

    STYLE = 0 | (1 << 7)                     # 无边框 + 不激活应用的面板
    LEVEL_FLOAT = 3
    BEHAVIOR = 1 | 16 | 64 | 256             # 所有桌面、固定、不参与切换、可浮在全屏上
    FILL = 2 | 16                            # 宽高随父视图

    def post(fn, *a):
        AppHelper.callAfter(fn, *a)

    core = Core(post)

    main_thread = threading.main_thread()

    def call_main(fn, *a):
        """本机网页的请求在后台线程到达；词本只在主线程改，这里切过去执行并等结果。"""
        if threading.current_thread() is main_thread:
            return fn(*a)
        box, ev = {}, threading.Event()

        def run():
            try:
                box["v"] = fn(*a)
            except Exception as ex:
                box["e"] = ex
            finally:
                ev.set()
        AppHelper.callAfter(run)
        if not ev.wait(5):
            raise RuntimeError("浮词正忙，稍后刷新")
        if "e" in box:
            raise box["e"]
        return box["v"]

    try:
        port = start_server(core, call_main)
        BASE_URL = f"http://127.0.0.1:{port}"
    except Exception:
        BASE_URL = None

    # 配色：清透玻璃上一律用白色的三个灰阶 + 一个青蓝强调色（系统深色模式的 cyan）
    def white(a):
        return NSColor.colorWithCalibratedWhite_alpha_(1.0, a)
    def black(a):
        return NSColor.colorWithCalibratedWhite_alpha_(0.0, a)
    LIGHT = THEME == "light"
    if LIGHT:
        FG, FG2, FG3 = black(0.88), black(0.55), black(0.34)
        ACCENT = NSColor.colorWithSRGBRed_green_blue_alpha_(0.0, 0.48, 1.0, 1.0)      # 系统蓝
        SMOKE_COLOR = NSColor.colorWithCalibratedWhite_alpha_(1.0, SMOKE)
        LINE_COLOR = black(0.08)
        APPEARANCE = "NSAppearanceNameAqua"
    else:
        FG, FG2, FG3 = white(0.96), white(0.64), white(0.40)
        ACCENT = NSColor.colorWithSRGBRed_green_blue_alpha_(0.39, 0.82, 1.0, 1.0)     # 青蓝
        SMOKE_COLOR = NSColor.colorWithCalibratedWhite_alpha_(0.0, SMOKE)
        LINE_COLOR = white(0.16)
        APPEARANCE = "NSAppearanceNameDarkAqua"
    SELECT_COLOR = ACCENT.colorWithAlphaComponent_(0.22 if LIGHT else 0.32)

    # 日志放数据文件夹：程序装在「应用程序」里时，app 内部不该被写入
    log_path = os.path.join(APP_DIR, "wordfloat.log")

    def log(*a):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(now().strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
        except Exception:
            pass

    def guard(fn, *a):
        try:
            return fn(*a)
        except Exception:
            import traceback
            log("ERROR", traceback.format_exc())

    # —— 外观工具 ——

    def font(size, weight=0.0):
        return NSFont.systemFontOfSize_weight_(size, weight)

    BADGE_H = 16
    BADGE_FONT = font(10.5, 0.4)

    def symbol(name, size, weight=0.23, color=None):
        try:
            from AppKit import NSImageSymbolConfiguration
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
            if img is None:
                return None
            conf = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight)
            if color is not None:
                conf = conf.configurationByApplyingConfiguration_(
                    NSImageSymbolConfiguration.configurationWithHierarchicalColor_(color))
            return img.imageWithSymbolConfiguration_(conf)
        except Exception:
            return None

    def attr(text, size, weight=0.0, color=None, italic=False, para=None, kern=None):
        f = font(size, weight)
        if italic:
            f = NSFontManager.sharedFontManager().convertFont_toHaveTrait_(f, 1)
        d = {NSFontAttributeName: f, NSForegroundColorAttributeName: color or FG}
        if para is not None:
            d[NSParagraphStyleAttributeName] = para
        if kern is not None:
            d[NSKernAttributeName] = kern
        return NSAttributedString.alloc().initWithString_attributes_(text, d)

    def para(before=0.0, line=2.0):
        p = NSMutableParagraphStyle.alloc().init()
        p.setParagraphSpacingBefore_(before)
        p.setLineSpacing_(line)
        return p

    def inside(frame, pad=6):
        p = NSEvent.mouseLocation()
        (x, y), (w, h) = frame
        return x - pad <= p.x <= x + w + pad and y - pad <= p.y <= y + h + pad

    try:
        EASE_OUT = objc.lookUpClass("CAMediaTimingFunction").functionWithControlPoints____(
            0.22, 1.0, 0.36, 1.0)       # 接近系统弹性动画的收尾
    except Exception:
        EASE_OUT = None

    try:
        GLASS = objc.lookUpClass("NSGlassEffectView")    # macOS 26 Liquid Glass
    except Exception:
        GLASS = None

    MASKS = {}

    def mask(radius):
        """旧系统毛玻璃用的可拉伸圆角蒙版。"""
        if radius in MASKS:
            return MASKS[radius][0]

        def draw(rect):
            NSColor.blackColor().set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill()
            return True
        d = radius * 2 + 1
        img = NSImage.imageWithSize_flipped_drawingHandler_((d, d), False, draw)
        img.setCapInsets_((radius, radius, radius, radius))
        img.setResizingMode_(1)
        MASKS[radius] = (img, draw)
        return img

    def haptic():
        try:
            from AppKit import NSHapticFeedbackManager
            NSHapticFeedbackManager.defaultPerformer().performFeedbackPattern_performanceTime_(0, 1)
        except Exception:
            pass

    # —— 视图 ——

    class Panel(NSPanel):
        def canBecomeKeyWindow(self):
            return True

        def canBecomeMainWindow(self):
            return False

    class Surface(NSView):
        """玻璃里的内容层。坐标从上往下，玻璃变高时上面的元素不动。负责拖动和右键。"""

        def isFlipped(self):
            return True

        def acceptsFirstMouse_(self, ev):
            return True

        def mouseDownCanMoveWindow(self):
            return False

        def mouseDown_(self, ev):
            p = NSEvent.mouseLocation()
            o = self.window().frame().origin
            self.drag = (p.x, p.y, o.x, o.y, False)

        def mouseDragged_(self, ev):
            d = getattr(self, "drag", None)
            if not d:
                return
            x0, y0, ox, oy, moved = d
            p = NSEvent.mouseLocation()
            dx, dy = p.x - x0, p.y - y0
            if moved or abs(dx) + abs(dy) > 3:
                self.drag = (x0, y0, ox, oy, True)
                self.window().setFrameOrigin_((ox + dx, oy + dy))

        def mouseUp_(self, ev):
            d = getattr(self, "drag", None)
            self.drag = None
            if d is not None and getattr(self, "owner", None):
                guard(self.owner.released, d[4])

        def rightMouseDown_(self, ev):
            if getattr(self, "owner", None):
                guard(self.owner.show_menu, ev, self)

    class Badge(NSView):
        """球右上角的待复习数：系统蓝胶囊，白字，一圈白边和图标分开。"""

        def hitTest_(self, p):
            return None                  # 点击、拖动都交给下面的球

        def drawRect_(self, rect):
            guard(self.draw_badge)

        @objc.python_method
        def draw_badge(self):
            b = self.bounds()
            h = b.size.height
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0.75, 0.75, b.size.width - 1.5, h - 1.5), (h - 1.5) / 2, (h - 1.5) / 2)
            ACCENT.setFill()
            path.fill()
            white(0.95).setStroke()
            path.setLineWidth_(1.5)
            path.stroke()
            t = getattr(self, "text", None)
            if t is not None:
                f = BADGE_FONT
                w = t.size().width
                # 数字按大写字高居中，而不是按整行高度
                y = (h - f.capHeight()) / 2 + f.descender()
                t.drawAtPoint_(((b.size.width - w) / 2, y))

    class FieldEditor(NSTextView):
        """输入框的编辑器：右键时给出浮词的菜单（含粘贴），而不是系统文本菜单。"""

        def menuForEvent_(self, ev):
            owner = getattr(self, "owner", None)
            if owner is None:
                return objc.super(FieldEditor, self).menuForEvent_(ev)
            m = NSMenu.alloc().init()
            m.addItemWithTitle_action_keyEquivalent_("粘贴", "paste:", "")
            m.addItem_(NSMenuItem.separatorItem())
            owner.fill_menu(m, clear=False)
            return m

    # —— 控制器 ——

    class Ball(NSObject):

        def init(self):
            self = objc.super(Ball, self).init()
            if self is None:
                return None
            self.state = "ball"          # ball / bar
            self.passive = False         # 复制即记录弹出的结果：不抢键盘
            self.anchor_right = True
            self.busy = 0
            self.typed = False
            self.prefill_cc = -1
            self.used_cc = -1
            self.hover_ticks = 0
            self.out_since = None
            self.need_exit = False
            self.result_h = 0
            self.hold_until = 0
            self.anim = 0
            self.build()
            return self

        # —— 搭界面 ——
        @objc.python_method
        def build(self):
            vf = NSScreen.mainScreen().visibleFrame()
            c = core.cfg.get("ball")
            if isinstance(c, list) and len(c) == 2:
                center = [c[0] + 23, c[1] + 23]           # 旧版存的是左下角
            else:
                center = None
            if not (center and self.on_screen(center)):
                center = [vf.origin.x + vf.size.width - BALL / 2 - 24,
                          vf.origin.y + vf.size.height * 0.62]
            self.center = self.snap(center)

            frame = self.ball_frame()
            p = Panel.alloc().initWithContentRect_styleMask_backing_defer_(frame, STYLE, 2, False)
            p.setLevel_(LEVEL_FLOAT)
            p.setCollectionBehavior_(BEHAVIOR)
            p.setOpaque_(False)
            p.setBackgroundColor_(NSColor.clearColor())
            p.setHasShadow_(True)
            p.setHidesOnDeactivate_(False)
            p.setFloatingPanel_(True)
            p.setBecomesKeyOnlyIfNeeded_(False)
            p.setReleasedWhenClosed_(False)
            p.setDelegate_(self)
            self.panel = p

            bounds = NSMakeRect(0, 0, frame.size.width, frame.size.height)
            self.surface = Surface.alloc().initWithFrame_(bounds)
            self.surface.owner = self
            self.surface.setAutoresizingMask_(FILL)
            smoke = NSBox.alloc().initWithFrame_(bounds)
            smoke.setBoxType_(4)
            smoke.setBorderWidth_(0)
            smoke.setCornerRadius_(BALL / 2)
            smoke.setFillColor_(SMOKE_COLOR)
            smoke.setTitlePosition_(0)
            smoke.setAutoresizingMask_(FILL)
            self.surface.addSubview_(smoke)
            self.smoke = smoke
            try:
                from AppKit import NSAppearance
                self.surface.setAppearance_(NSAppearance.appearanceNamed_(APPEARANCE))
            except Exception:
                pass
            # 深色外观：白字配清透玻璃，像系统的控制中心
            try:
                from AppKit import NSAppearance
                p.setAppearance_(NSAppearance.appearanceNamed_(APPEARANCE))
            except Exception:
                pass
            if GLASS is not None:
                g = GLASS.alloc().initWithFrame_(bounds)
                g.setCornerRadius_(BALL / 2)
                if GLASS_CLEAR:
                    try:
                        g.setStyle_(1)                  # 清透
                        log("glass style", g.style())
                    except Exception as ex:
                        log("glass style unsupported", ex)
                if GLASS_TINT > 0:
                    try:
                        g.setTintColor_(NSColor.colorWithCalibratedWhite_alpha_(0.0, GLASS_TINT))
                    except Exception:
                        pass
                g.setContentView_(self.surface)
                self.glass = g
                p.setContentView_(g)
            else:
                v = NSVisualEffectView.alloc().initWithFrame_(bounds)
                v.setMaterial_(13)                  # HUD：深色半透明
                v.setBlendingMode_(0)
                v.setState_(1)
                v.setMaskImage_(mask(BALL // 2))
                v.addSubview_(self.surface)
                self.glass = v
                p.setContentView_(v)
            log("material", "liquid-glass" if GLASS is not None else "visual-effect")

            self.img_ball = symbol("character.book.closed", 19, 0.3)
            self.img_search = symbol("magnifyingglass", 18, 0.3)

            self.icon = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 24, 24))
            self.icon.setContentTintColor_(FG)
            self.surface.addSubview_(self.icon)

            s = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(0, 0, 16, 16))
            s.setStyle_(1)
            s.setControlSize_(1)
            s.setDisplayedWhenStopped_(False)
            self.spinner = s
            self.surface.addSubview_(s)

            f = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 28))
            f.setBezeled_(False)
            f.setBordered_(False)
            f.setDrawsBackground_(False)
            f.setFocusRingType_(1)
            f.setFont_(font(21, 0.0))
            f.setTextColor_(FG)
            f.setPlaceholderAttributedString_(attr("查找单词", 21, 0.0, FG3))
            f.cell().setUsesSingleLineMode_(True)
            f.cell().setScrollable_(True)
            f.setTarget_(self)
            f.setAction_("submit:")
            f.setDelegate_(self)
            f.setHidden_(True)
            self.field = f
            self.surface.addSubview_(f)

            rb = NSButton.buttonWithTitle_target_action_("", self, "reviewToday:")
            rb.setBordered_(False)
            rb.setHidden_(True)
            self.review_btn = rb
            self.surface.addSubview_(rb)

            self.editor = FieldEditor.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
            self.editor.setFieldEditor_(True)
            self.editor.owner = self
            try:
                from AppKit import NSBackgroundColorAttributeName
                self.editor.setInsertionPointColor_(FG)
                self.editor.setSelectedTextAttributes_({
                    NSBackgroundColorAttributeName: SELECT_COLOR,
                    NSForegroundColorAttributeName: FG})
            except Exception:
                pass

            line = NSBox.alloc().initWithFrame_(NSMakeRect(PAD, BAR_H, BAR_W - 2 * PAD, 1))
            line.setBoxType_(4)                     # 自定义：纯色细线
            line.setBorderWidth_(0)
            line.setFillColor_(LINE_COLOR)
            line.setTitlePosition_(0)
            line.setHidden_(True)
            self.line = line
            self.surface.addSubview_(line)

            lbl = NSTextField.wrappingLabelWithString_("")
            lbl.setSelectable_(False)
            lbl.setHidden_(True)
            self.result = lbl
            self.surface.addSubview_(lbl)

            self.badge = Badge.alloc().initWithFrame_(NSMakeRect(0, 0, 16, 16))
            self.badge.setHidden_(True)
            self.badge_label = None
            self.ticks = 0
            self.surface.addSubview_(self.badge)

            self.layout_ball()
            p.orderFrontRegardless()
            guard(self.build_status_item)
            guard(self.brand_icon)

            self.last_cc = NSPasteboard.generalPasteboard().changeCount()
            self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.6, self, "tick:", None, True)
            self.poll = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.08, self, "pollMouse:", None, True)
            self.slow = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1800, self, "periodic:", None, True)
            AppHelper.callLater(1.5, self.periodic_, None)
            threading.Thread(target=guard, args=(self.housekeeping,), daemon=True).start()
            log("started", core.cfg["preset"], "has_key", core.has_key())
            if not core.has_key():
                AppHelper.callLater(0.8, self.ask_key)

        @objc.python_method
        def housekeeping(self):
            """启动时：备份生词，刷新开机启动。都是文件操作，放后台线程。"""
            log("backup", backup_words())
            if core.cfg.get("autostart", True):
                log("autostart", install_autostart())

        @objc.python_method
        def refresh_badge(self):
            st = core.stats()
            n = st["due"] + st["practice"]
            show = n > 0 and self.state == "ball"
            if show:
                label = "99+" if n > 99 else str(n)
                if label != self.badge_label:
                    self.badge_label = label
                    self.badge.text = NSAttributedString.alloc().initWithString_attributes_(
                        label, {NSFontAttributeName: BADGE_FONT,
                                NSForegroundColorAttributeName: white(1.0)})
                    w = max(BADGE_H, round(self.badge.text.size().width) + 9)
                    # 右端圆心放在球内 45° 方向，整个角标不被球的圆边切掉
                    right = BALL / 2 + 19
                    self.badge.setFrame_(NSMakeRect(right - w, BALL / 2 - 19, w, BADGE_H))
                    self.badge.setNeedsDisplay_(True)
            self.badge.setHidden_(not show)

        @objc.python_method
        def brand_icon(self):
            """弹窗和系统里显示浮词图标，而不是 Python 火箭。在 app 里运行时用 app 自带的图标。"""
            icns = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AppIcon.icns")
            img = NSImage.alloc().initWithContentsOfFile_(icns) if os.path.exists(icns) else None
            if img is None:
                img = symbol("character.book.closed.fill", 64, 0.3, NSColor.controlAccentColor())
            if img is not None:
                self.app_icon = img
                NSApplication.sharedApplication().setApplicationIconImage_(img)

        @objc.python_method
        def on_screen(self, c):
            for sc in NSScreen.screens():
                (x, y), (w, h) = sc.visibleFrame()
                if x + 10 <= c[0] <= x + w - 10 and y + 10 <= c[1] <= y + h - 10:
                    return True
            return False

        @objc.python_method
        def snap(self, c):
            """把球心移到离它最近的屏幕边缘（左、右、上、下取最近的一边）。"""
            vf = None
            for sc in NSScreen.screens():
                (x, y), (w, h) = sc.frame()
                if x <= c[0] <= x + w and y <= c[1] <= y + h:
                    vf = sc.visibleFrame()
                    break
            if vf is None:
                vf = NSScreen.mainScreen().visibleFrame()
            (x, y), (w, h) = vf
            r = BALL / 2 + EDGE_MARGIN
            lo_x, hi_x, lo_y, hi_y = x + r, x + w - r, y + r, y + h - r
            cx = min(max(c[0], lo_x), hi_x)
            cy = min(max(c[1], lo_y), hi_y)
            dist = {"left": cx - lo_x, "right": hi_x - cx, "bottom": cy - lo_y, "top": hi_y - cy}
            side = min(dist, key=dist.get)
            if side == "left":
                cx = lo_x
            elif side == "right":
                cx = hi_x
            elif side == "bottom":
                cy = lo_y
            else:
                cy = hi_y
            return [cx, cy]

        @objc.python_method
        def screen_frame(self):
            sc = self.panel.screen() if getattr(self, "panel", None) is not None else None
            return (sc or NSScreen.mainScreen()).visibleFrame()

        # —— 三种形态的尺寸 ——
        @objc.python_method
        def ball_frame(self):
            cx, cy = self.center
            return NSMakeRect(cx - BALL / 2, cy - BALL / 2, BALL, BALL)

        @objc.python_method
        def bar_frame(self, extra=0):
            cx, cy = self.center
            vf = self.screen_frame()
            self.anchor_right = cx > vf.origin.x + vf.size.width / 2
            x = cx + BAR_H / 2 - BAR_W if self.anchor_right else cx - BAR_H / 2
            h = BAR_H + extra
            top = cy + BAR_H / 2
            y = top - h
            # 不出屏幕
            x = min(max(x, vf.origin.x + 8), vf.origin.x + vf.size.width - BAR_W - 8)
            if y < vf.origin.y + 8:
                y = vf.origin.y + 8
            if y + h > vf.origin.y + vf.size.height - 4:
                y = vf.origin.y + vf.size.height - 4 - h
            return NSMakeRect(x, y, BAR_W, h)

        @objc.python_method
        def set_radius(self, r):
            if getattr(self, "smoke", None) is not None:
                self.smoke.setCornerRadius_(r)
            if GLASS is not None:
                self.glass.setCornerRadius_(r)
            else:
                self.glass.setMaskImage_(mask(int(r)))

        # —— 布局（坐标从上往下） ——
        @objc.python_method
        def layout_ball(self):
            c = (BALL - 24) / 2
            self.icon.setFrame_(NSMakeRect(c, c, 24, 24))
            if self.img_ball is not None:
                self.icon.setImage_(self.img_ball)
            self.icon.setContentTintColor_(ACCENT)
            self.icon.setHidden_(self.busy > 0)
            self.spinner.setFrame_(NSMakeRect((BALL - 16) / 2, (BALL - 16) / 2, 16, 16))
            self.field.setHidden_(True)
            self.review_btn.setHidden_(True)
            self.line.setHidden_(True)
            self.result.setHidden_(True)
            guard(self.refresh_badge)

        @objc.python_method
        def layout_bar(self):
            y = (BAR_H - 24) / 2
            self.icon.setFrame_(NSMakeRect(PAD - 2, y, 24, 24))
            if self.img_search is not None:
                self.icon.setImage_(self.img_search)
            self.icon.setContentTintColor_(FG2)
            self.icon.setHidden_(self.busy > 0)
            self.badge.setHidden_(True)
            self.spinner.setFrame_(NSMakeRect(PAD + 2, (BAR_H - 16) / 2, 16, 16))
            fx = PAD + 32
            right = BAR_W - PAD
            st = core.stats()
            due = st["due"] + st["practice"]
            if due:
                self.review_btn.setAttributedTitle_(attr(f"复习 {due}", 14, 0.4, ACCENT))
                w = self.review_btn.attributedTitle().size().width + 6
                self.review_btn.setFrame_(NSMakeRect(right - w, (BAR_H - 24) / 2, w, 24))
                self.review_btn.setToolTip_(f"今天 {st['due']} 个词到期，另有 {st['practice']} 张写作 / 口语练习")
                self.review_btn.setHidden_(False)
                right -= w + 12
            else:
                self.review_btn.setHidden_(True)
            self.field.setFrame_(NSMakeRect(fx, (BAR_H - 28) / 2, right - fx, 28))

        # —— 动画 ——
        @objc.python_method
        def animate(self, frame, dur, after=None):
            win = self.panel
            self.anim = time.time() + dur + 0.3

            def group(ctx):
                ctx.setDuration_(dur)
                ctx.setAllowsImplicitAnimation_(True)
                if EASE_OUT is not None:
                    ctx.setTimingFunction_(EASE_OUT)
                win.animator().setFrame_display_(frame, True)

            def done():
                self.anim = 0
                win.invalidateShadow()
                if after:
                    guard(after)
            try:
                NSAnimationContext.runAnimationGroup_completionHandler_(group, done)
            except Exception:
                win.setFrame_display_(frame, True)
                done()

        # —— 展开 / 收起 ——
        @objc.python_method
        def expand(self, passive=False):
            if self.state == "bar":
                if self.passive and not passive:
                    self.activate()
                return
            log("expand", "passive" if passive else "")
            self.state = "bar"
            self.passive = passive
            self.out_since = None
            self.result_h = 0
            self.set_radius(BAR_H / 2)
            self.layout_bar()
            if not passive:
                self.prefill()
            self.animate(self.bar_frame(), 0.34, self.expanded_done)

        @objc.python_method
        def expanded_done(self):
            if self.state != "bar":
                return
            self.field.setAlphaValue_(0.0)
            self.field.setHidden_(False)
            self.field.animator().setAlphaValue_(1.0)
            if not self.passive:
                self.activate()

        @objc.python_method
        def activate(self):
            self.passive = False
            self.panel.makeKeyAndOrderFront_(None)
            self.panel.makeFirstResponder_(self.field)
            ed = self.field.currentEditor()
            if ed is not None:
                ed.selectAll_(None)

        @objc.python_method
        def collapse(self):
            if self.state != "bar":
                return
            log("collapse")
            fr = self.panel.frame()
            # 用户拖动过展开的条：按它现在的位置换算球心
            if self.anchor_right:
                self.center = [fr.origin.x + BAR_W - BAR_H / 2, fr.origin.y + fr.size.height - BAR_H / 2]
            else:
                self.center = [fr.origin.x + BAR_H / 2, fr.origin.y + fr.size.height - BAR_H / 2]
            self.center = self.snap(self.center)
            self.save_pos()
            self.state = "ball"
            self.passive = False
            self.typed = False
            self.need_exit = inside(self.ball_frame(), 0)
            self.field.setStringValue_("")
            self.result_h = 0
            self.layout_ball()
            # 交还键盘：先撤下，再不抢焦点地放回
            self.panel.orderOut_(None)
            self.panel.orderFrontRegardless()
            self.animate(self.ball_frame(), 0.26, lambda: self.set_radius(BALL / 2))

        @objc.python_method
        def prefill(self):
            pb = NSPasteboard.generalPasteboard()
            cc = pb.changeCount()
            w = clip_word(pb.stringForType_(NSPasteboardTypeString))
            if w and cc != self.used_cc:
                self.field.setStringValue_(w)
                self.prefill_cc = cc
            else:
                self.field.setStringValue_("")
                self.prefill_cc = -1
            self.typed = False

        # —— 结果区：同一块玻璃向下长出 ——
        @objc.python_method
        def show_text(self, text, hold=False, quiet=False):
            if self.state != "bar":
                self.expand(passive=True)
            w = BAR_W - 2 * PAD - 2
            self.result.setAttributedStringValue_(text)
            h = self.result.cell().cellSizeForBounds_(NSMakeRect(0, 0, w, 10000)).height
            h = min(max(h, 18), 460)
            self.result.setFrame_(NSMakeRect(PAD + 1, BAR_H + 14, w, h))
            self.result_h = h + 14 + 20
            self.line.setFrame_(NSMakeRect(PAD, BAR_H - 0.5, BAR_W - 2 * PAD, 1))
            self.line.setHidden_(False)
            if quiet and not self.result.isHidden():
                self.animate(self.bar_frame(self.result_h), 0.24)   # 补充内容：只平滑长高，不闪
            else:
                self.result.setAlphaValue_(0.0)
                self.result.setHidden_(False)
                self.animate(self.bar_frame(self.result_h), 0.32)
                self.result.animator().setAlphaValue_(1.0)
            if hold or self.passive:
                self.hold_until = time.time() + PASSIVE_HOLD

        @objc.python_method
        def note(self, text, hold=True):
            self.show_text(attr(text, 14, 0.0, FG2, para=para(0, 3)), hold)

        @objc.python_method
        def show_entry(self, e, err, cached, quiet=False):
            sec = FG2
            s = NSMutableAttributedString.alloc().init()
            if e is None:
                return self.note(err or "")
            self.shown_key = e["key"]
            n = len(e.get("lookups", []))
            s.appendAttributedString_(attr(e["lemma"], 26, 0.3, kern=-0.3))
            if e.get("phonetic"):
                s.appendAttributedString_(attr("\n" + e["phonetic"], 14, 0.0, sec, para=para(2)))
                if n > 1:
                    s.appendAttributedString_(attr("   ·   ", 14, 0.0, FG3))
            elif n > 1:
                s.appendAttributedString_(attr("\n", 14, 0.0, sec, para=para(2)))
            if n > 1:
                s.appendAttributedString_(attr(f"第 {n} 次遇到", 13, 0.4, ACCENT))
            groups = []
            for sense in e.get("senses") or []:
                lab = pos_label(sense.get("pos"))
                if groups and groups[-1][0] == lab:
                    groups[-1][1].append(sense.get("zh") or "")
                else:
                    groups.append((lab, [sense.get("zh") or ""]))
            for lab, zhs in groups:
                s.appendAttributedString_(attr("\n" + lab, 12, 0.4, sec, para=para(14), kern=0.3))
                for z in zhs:
                    s.appendAttributedString_(attr("\n" + z, 16, 0.0, FG, para=para(3, 3)))
            if e.get("use"):
                s.appendAttributedString_(attr("\n考试  ", 12, 0.4, ACCENT, para=para(14, 3)))
                s.appendAttributedString_(attr(e["use"], 13, 0.0, FG, para=para(14, 3)))
            if e.get("note"):
                s.appendAttributedString_(attr("\n" + e["note"], 13, 0.0, sec, para=para(8 if e.get("use") else 14, 3)))
            ex = e.get("example") or {}
            if ex.get("en"):
                s.appendAttributedString_(attr("\n" + ex["en"], 14, 0.0, FG, italic=True, para=para(12, 3)))
                if ex.get("zh"):
                    s.appendAttributedString_(attr("\n" + ex["zh"], 13, 0.0, sec, para=para(3, 3)))
            if not err and core.needs_enrich(e) and core.has_key():
                s.appendAttributedString_(attr("\n记忆点和例句生成中…", 12, 0.0, FG3, para=para(14, 3)))
            if err:
                s.appendAttributedString_(attr("\n" + err.rstrip("。") + "。单词已记下，释义会自动补全。", 12, 0.0, sec, para=para(14, 3)))
            elif not e.get("senses"):
                s.appendAttributedString_(attr("\n释义待补全。", 13, 0.0, sec, para=para(10)))
            self.show_text(s, quiet=quiet)
            if not err and not quiet:
                haptic()

        # —— 鼠标：轮询位置，比依赖进出事件更稳 ——
        def pollMouse_(self, timer):
            guard(self.poll_mouse)

        @objc.python_method
        def poll_mouse(self):
            if self.anim and time.time() < self.anim:
                return
            on = inside(self.panel.frame(), 0 if self.state == "ball" else 6)
            if self.need_exit:
                if not on:
                    self.need_exit = False
                return
            if self.state == "ball":
                self.hover_ticks = self.hover_ticks + 1 if on else 0
                if self.hover_ticks * 0.08 >= HOVER_DELAY:
                    self.hover_ticks = 0
                    self.expand()
                return
            if on:
                self.out_since = None
                if self.passive:
                    self.activate()
                return
            if time.time() < self.hold_until or self.busy:
                return
            t = time.time()
            if self.out_since is None:
                self.out_since = t
            elif t - self.out_since >= LEAVE_DELAY:
                if self.typed and self.field.stringValue().strip():
                    return          # 正在打字，不打断；按 Esc 或回车结束
                self.collapse()

        @objc.python_method
        def released(self, moved):
            if moved:
                if self.state == "ball":
                    fr = self.panel.frame()
                    self.center = self.snap([fr.origin.x + BALL / 2, fr.origin.y + BALL / 2])
                    self.save_pos()
                    self.animate(self.ball_frame(), 0.32)
            elif self.state == "ball":
                self.expand()
            else:
                self.activate()

        # —— 输入框 ——
        def windowWillReturnFieldEditor_toObject_(self, win, obj):
            ed = getattr(self, "editor", None)
            if ed is not None and obj is getattr(self, "field", None):
                return ed
            return None

        def controlTextDidChange_(self, note):
            self.typed = True

        def control_textView_doCommandBySelector_(self, control, tv, sel):
            name = sel.decode() if isinstance(sel, bytes) else str(sel)
            if name == "cancelOperation:":
                self.collapse()
                return True
            return False

        def submit_(self, sender):
            guard(self.do_submit)

        @objc.python_method
        def do_submit(self):
            text = self.field.stringValue()
            if not text.strip():
                return
            if self.prefill_cc >= 0:
                self.used_cc = self.prefill_cc
            self.start_lookup(text, from_clip=False)

        @objc.python_method
        def start_lookup(self, text, from_clip):
            def done(e, err, cached):
                self.set_busy(-1)
                if err and e is not None:
                    AppHelper.callLater(45, core.fill_pending)
                if not from_clip and self.state == "bar":
                    ed = self.field.currentEditor()
                    if ed is not None:
                        ed.selectAll_(None)     # 保留刚查的词并全选，直接输入就是下一个
                    self.typed = False
                if from_clip:
                    self.field.setStringValue_(e["lemma"] if e else text)
                self.show_entry(e, err, cached)
            def enriched(e):
                # 记忆点和例句到了：如果卡片还在显示这个词，就地刷新
                if self.state == "bar" and getattr(self, "shown_key", None) == e["key"]:
                    self.show_entry(e, None, True, quiet=True)
            self.set_busy(+1)
            if not core.lookup(text, done, enriched):
                self.set_busy(-1)

        @objc.python_method
        def set_busy(self, d):
            self.busy = max(0, self.busy + d)
            if self.busy:
                self.spinner.startAnimation_(None)
                self.icon.setHidden_(True)
            else:
                self.spinner.stopAnimation_(None)
                self.icon.setHidden_(False)

        # —— 剪贴板 ——
        def tick_(self, timer):
            guard(self.clip_tick)
            # 复习页里每答一张卡，角标在 5 秒内跟着变
            self.ticks += 1
            if self.ticks % 8 == 0 and self.state == "ball":
                guard(self.refresh_badge)

        @objc.python_method
        def clip_tick(self):
            pb = NSPasteboard.generalPasteboard()
            cc = pb.changeCount()
            if cc == self.last_cc:
                return
            self.last_cc = cc
            if not core.cfg.get("watch_clipboard"):
                return
            w = clip_word(pb.stringForType_(NSPasteboardTypeString))
            if w:
                self.used_cc = cc
                self.start_lookup(w, from_clip=True)

        @objc.python_method
        def save_pos(self):
            core.cfg["ball"] = [round(self.center[0] - 23), round(self.center[1] - 23)]
            core.save_cfg()

        # —— 菜单 ——
        @objc.python_method
        def fill_menu(self, m, clear=True):
            if clear:
                m.removeAllItems()
            m.setAutoenablesItems_(False)

            def item(title, action, icon=None, state=None):
                it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
                it.setTarget_(self)
                if icon:
                    img = symbol(icon, 13, 0.0)
                    if img is not None:
                        it.setImage_(img)
                if state is not None:
                    it.setState_(1 if state else 0)
                m.addItem_(it)
                return it
            st = core.stats()
            due = st["due"] + st["practice"]
            item(f"今日复习（{due}）" if due else "今日复习", "reviewToday:", "rectangle.stack")
            item("生词本", "wordbook:", "books.vertical")
            wst = st.get("weekly") or {}
            left = sum(1 for k in ("write", "speak") if wst.get(k) is None)
            item(f"本周练习（还差 {left} 项）" if left else "本周练习 ✓", "weekly:", "calendar")
            m.addItem_(NSMenuItem.separatorItem())
            item("复制即记录", "toggleWatch:", "doc.on.clipboard", bool(core.cfg.get("watch_clipboard")))
            item("开机自动打开", "toggleAutostart:", "sunrise", autostart_on())
            sub = NSMenu.alloc().init()
            for i, name in enumerate(PRESETS):
                it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(name, "choosePreset:", "")
                it.setTarget_(self)
                it.setTag_(i)
                it.setState_(1 if core.cfg["preset"] == name else 0)
                sub.addItem_(it)
            svc = item("模型服务", None, "cpu")
            svc.setSubmenu_(sub)
            item("设置 API Key…", "setKey:", "key")
            m.addItem_(NSMenuItem.separatorItem())
            item("打开数据文件夹", "openFolder:", "folder")
            item("导出 CSV（可导入 Anki）", "exportCsv:", "square.and.arrow.up")
            m.addItem_(NSMenuItem.separatorItem())
            item("退出浮词", "quit:", "power")
            return m

        @objc.python_method
        def show_menu(self, ev, view):
            m = self.fill_menu(NSMenu.alloc().init())
            NSMenu.popUpContextMenu_withEvent_forView_(m, ev, view)

        def menuNeedsUpdate_(self, menu):
            guard(self.fill_menu, menu)

        @objc.python_method
        def build_status_item(self):
            from AppKit import NSStatusBar
            self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
            btn = self.status_item.button()
            img = symbol("character.book.closed", 15)
            if img is not None:
                img.setTemplate_(True)
                btn.setImage_(img)
            else:
                btn.setTitle_("词")
            btn.setToolTip_(APP_NAME)
            menu = NSMenu.alloc().init()
            menu.setDelegate_(self)
            self.fill_menu(menu)
            self.status_item.setMenu_(menu)

        @objc.python_method
        def open_page(self, path):
            if BASE_URL is None:
                self.note("复习页启动失败，请重启浮词。")
                return
            if self.state == "bar":
                self.collapse()
            url = BASE_URL + path
            # Safari 的语音识别走苹果服务，国内可用；Chrome 的走 Google
            try:
                subprocess.Popen(["open", "-a", "Safari", url])
            except Exception:
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))

        def reviewToday_(self, sender):
            self.open_page("/review")

        def wordbook_(self, sender):
            self.open_page("/book")

        def weekly_(self, sender):
            self.open_page("/weekly")

        def toggleWatch_(self, sender):
            on = not core.cfg.get("watch_clipboard")
            core.cfg["watch_clipboard"] = on
            core.save_cfg()
            self.last_cc = NSPasteboard.generalPasteboard().changeCount()
            self.note("复制即记录已开启。复制英文单词，自动查词。" if on else "复制即记录已关闭。")

        def toggleAutostart_(self, sender):
            on = not autostart_on()
            try:
                install_autostart() if on else remove_autostart()
            except Exception as ex:
                log("autostart error", ex)
                self.note(f"设置失败：{ex}")
                return
            core.cfg["autostart"] = on
            core.save_cfg()
            self.note("登录 Mac 后，浮词会自动打开。" if on else "已关闭开机自动打开。")

        def choosePreset_(self, sender):
            name = list(PRESETS)[sender.tag()]
            core.cfg["preset"] = name
            core.save_cfg()
            if not core.has_key():
                self.ask_key()
            else:
                self.note(f"已切换到 {name}。")

        def setKey_(self, sender):
            self.ask_key()

        def openFolder_(self, sender):
            open_path(APP_DIR)

        def exportCsv_(self, sender):
            path = core.export_csv()
            subprocess.Popen(["open", "-R", path])

        def quit_(self, sender):
            NSApplication.sharedApplication().terminate_(None)

        # —— API Key ——
        @objc.python_method
        def ask_key(self):
            name = core.cfg["preset"]
            base, model, site = PRESETS[name]
            while True:
                a = NSAlert.alloc().init()
                a.setMessageText_(f"{name} API Key")
                info = (f"在 {site.replace('https://', '').rstrip('/')} 注册，"
                        "进入「API Keys」创建，然后粘贴到下面。")
                if name == FREE_PRESET:
                    info = ("GLM-4.7-Flash 可免费调用。\n" + info +
                            "\n完整的 Key 中间有一个点，请整段复制。")
                a.setInformativeText_(info)
                if getattr(self, "app_icon", None) is not None:
                    a.setIcon_(self.app_icon)
                fld = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
                fld.setStringValue_(core.cfg["keys"].get(name, ""))
                fld.setPlaceholderString_("xxxxxxxx.xxxxxxxx")
                a.setAccessoryView_(fld)
                a.addButtonWithTitle_("保存")
                a.addButtonWithTitle_("取消")
                a.addButtonWithTitle_("获取 Key…")
                a.window().setInitialFirstResponder_(fld)
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                r = a.runModal()
                if r == 1002:
                    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(site))
                    continue
                if r == 1000:
                    key = fld.stringValue().strip()
                    core.cfg["keys"][name] = key
                    core.save_cfg()
                    if key:
                        self.note("正在验证…")
                        core.ping(self.ping_done)
                break

        @objc.python_method
        def ping_done(self, err):
            if err:
                self.note(err)
            else:
                self.note("连接正常。鼠标停在球上，回车即可查词。")
                core.fill_pending()
                self.periodic_(None)

        # —— 定时：补全待查词和考试用法 ——
        def periodic_(self, timer):
            core.fill_pending()
            guard(backup_words)           # 跨过午夜也会补上当天的备份
            guard(self.refresh_badge)

    # —— 主菜单：没有它，输入框里 ⌘V 粘贴不了 ——
    def edit_menu():
        main = NSMenu.alloc().init()
        app_item = NSMenuItem.alloc().init()
        main.addItem_(app_item)
        app_menu = NSMenu.alloc().init()
        app_menu.addItemWithTitle_action_keyEquivalent_("退出浮词", "terminate:", "q")
        app_item.setSubmenu_(app_menu)
        edit_item = NSMenuItem.alloc().init()
        main.addItem_(edit_item)
        edit = NSMenu.alloc().initWithTitle_("编辑")
        for title, sel, key in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("拷贝", "copy:", "c"),
                                ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")]:
            edit.addItemWithTitle_action_keyEquivalent_(title, sel, key)
        edit_item.setSubmenu_(edit)
        return main

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)        # 不在程序坞里出现
    app.setMainMenu_(edit_menu())
    ball = Ball.alloc().init()
    run_mac.ball = ball                # 保持引用
    AppHelper.runEventLoop(installInterrupt=True)


def single_instance():
    import fcntl
    os.makedirs(APP_DIR, exist_ok=True)
    f = open(os.path.join(APP_DIR, ".lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f


def main():
    if sys.platform != "darwin":
        sys.exit("浮词悬浮球目前只支持 macOS。")
    lock = single_instance()
    if lock is None:
        sys.exit(0)    # 已经在运行
    os.makedirs(APP_DIR, exist_ok=True)
    try:
        run_mac()
    except ImportError:
        # 组件坏了：让 app 启动器下次重新检查、重新安装
        try:
            os.remove(os.path.join(APP_DIR, ".python"))
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()
