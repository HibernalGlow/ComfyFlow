#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_workflow_recursive.py
============================
递归批量调用 ComfyUI 单图工作流，将输入目录树中的每张图片处理后
以相同的父子结构输出到目标目录。

使用方式
--------
  # 无参数 → 自动读取/生成配置文件，进入交互式引导菜单
  python batch_workflow_recursive.py

  # 指定配置文件
  python batch_workflow_recursive.py --config my.toml

  # 跳过菜单，直接按配置文件内容运行（可配合 --input-dir 等覆盖单项）
  python batch_workflow_recursive.py --no-interactive

  # 命令行完整传参（覆盖配置文件中的对应值，自动跳过菜单）
  python batch_workflow_recursive.py \
      --workflow "Workflow/Z-image超真实动漫转真人.json" \
      --input-dir D:/anime --output-dir D:/real \
      --no-interactive
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# TOML 支持（读取）
# ---------------------------------------------------------------------------
try:
    import tomllib          # Python 3.11+ 标准库
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # pip install tomli  (3.9 / 3.10)
    except ModuleNotFoundError:
        tomllib = None           # 最终回退：内置手写解析器


# ===========================================================================
# 一、配置项 Schema
# ===========================================================================
# 每条: (flat_key, toml_section, toml_key, 中文标签, 类型, 默认值, 是否必填)
_SCHEMA = [
    ("server",            "comfyui", "server",            "服务器地址",   str,   "http://127.0.0.1:8000", False),
    ("workflow",          "paths",   "workflow",           "工作流文件",   str,   "",                      True),
    ("input_dir",         "paths",   "input_dir",          "输入目录",     str,   "",                      True),
    ("output_dir",        "paths",   "output_dir",         "输出目录",     str,   "",                      True),
    ("sleep",             "options", "sleep",              "任务间隔(秒)", float, 0.0,                     False),
    ("skip_existing",     "options", "skip_existing",      "跳过已存在",   bool,  True,                    False),
    ("continue_on_error", "options", "continue_on_error",  "出错时继续",   bool,  True,                    False),
    ("timeout",           "options", "timeout",            "超时秒数/张",  int,   3600,                    False),
]

SCHEMA_KEYS   = [s[0] for s in _SCHEMA]
REQUIRED_KEYS = {s[0] for s in _SCHEMA if s[6]}
_SEC_OF       = {s[0]: s[1] for s in _SCHEMA}
_TKEY_OF      = {s[0]: s[2] for s in _SCHEMA}
_LABEL_OF     = {s[0]: s[3] for s in _SCHEMA}
_TYPE_OF      = {s[0]: s[4] for s in _SCHEMA}
_DEFAULT_OF   = {s[0]: s[5] for s in _SCHEMA}

# 配置文件默认位置：与脚本同目录，同名 .toml
DEFAULT_CONFIG_PATH = Path(__file__).with_suffix(".toml")


# ===========================================================================
# 二、TOML 读写工具
# ===========================================================================

def _parse_toml_naive(text: str) -> Dict[str, Any]:
    """内置 mini TOML 解析器（仅处理本脚本用到的简单结构）。"""
    result: Dict[str, Any] = {}
    section: Dict[str, Any] = result
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            result.setdefault(name, {})
            section = result[name]
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if (v.startswith('"') and v.endswith('"')) or \
               (v.startswith("'") and v.endswith("'")):
                section[k] = v[1:-1]
            elif v.lower() == "true":
                section[k] = True
            elif v.lower() == "false":
                section[k] = False
            else:
                try:
                    section[k] = int(v)
                except ValueError:
                    try:
                        section[k] = float(v)
                    except ValueError:
                        section[k] = v
    return result


def _load_toml_raw(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(text)
    return _parse_toml_naive(text)


def _toml_scalar(v: Any) -> str:
    """把 Python 值序列化为 TOML 标量字符串。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(v)


def save_toml(path: Path, cfg: Dict[str, Any]) -> None:
    """将配置 dict 写回 TOML 文件（保留注释头部）。"""
    lines = [
        "# ComfyUI 递归批量处理 - 配置文件",
        "# 可直接编辑此文件，或运行脚本进入引导式配置界面",
        "",
    ]
    seen_sections: List[str] = []
    section_rows: Dict[str, List[Tuple[str, str]]] = {}

    for key in SCHEMA_KEYS:
        sec  = _SEC_OF[key]
        tkey = _TKEY_OF[key]
        val  = cfg.get(key, _DEFAULT_OF[key])
        if sec not in section_rows:
            section_rows[sec] = []
            seen_sections.append(sec)
        section_rows[sec].append((tkey, _toml_scalar(val)))

    for sec in seen_sections:
        lines.append(f"[{sec}]")
        kw = max(len(k) for k, _ in section_rows[sec])
        for tkey, tval in section_rows[sec]:
            lines.append(f"{tkey:<{kw}} = {tval}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ===========================================================================
# 三、配置加载 & 合并
# ===========================================================================

def default_cfg() -> Dict[str, Any]:
    return {k: _DEFAULT_OF[k] for k in SCHEMA_KEYS}


def cfg_from_toml(path: Path) -> Dict[str, Any]:
    raw = _load_toml_raw(path)
    cfg = default_cfg()
    for key in SCHEMA_KEYS:
        sec  = _SEC_OF[key]
        tkey = _TKEY_OF[key]
        if sec in raw and tkey in raw[sec]:
            cfg[key] = raw[sec][tkey]
    return cfg


def missing_fields(cfg: Dict[str, Any]) -> List[str]:
    return [k for k in REQUIRED_KEYS if not str(cfg.get(k, "")).strip()]


# ===========================================================================
# 四、终端显示工具（处理中文双宽字符）
# ===========================================================================

def _dw(s: str) -> int:
    """计算字符串的终端显示宽度（CJK 全宽字符算 2 列）。"""
    w = 0
    for c in s:
        cp = ord(c)
        if (0x1100 <= cp <= 0x115F
                or 0x2329 <= cp <= 0x232A
                or 0x2E80 <= cp <= 0x303E
                or 0x3040 <= cp <= 0x33FF
                or 0x3400 <= cp <= 0x4DBF
                or 0x4E00 <= cp <= 0xA4CF
                or 0xA960 <= cp <= 0xA97F
                or 0xAC00 <= cp <= 0xD7FF
                or 0xF900 <= cp <= 0xFAFF
                or 0xFE10 <= cp <= 0xFE1F
                or 0xFE30 <= cp <= 0xFE6F
                or 0xFF01 <= cp <= 0xFF60
                or 0xFFE0 <= cp <= 0xFFE6
                or 0x1B000 <= cp <= 0x1B77F
                or 0x1F300 <= cp <= 0x1FA6F
                or 0x20000 <= cp <= 0x2A6DF):
            w += 2
        else:
            w += 1
    return w


def _pad(s: str, width: int, fill: str = " ", align: str = "left") -> str:
    """按显示宽度对齐填充字符串。"""
    d = _dw(s)
    pad = max(0, width - d) * fill
    return (s + pad) if align == "left" else (pad + s)


# ===========================================================================
# 五、交互式配置菜单
# ===========================================================================

_MW = 70   # 菜单显示总宽（列数）

_BOOL_YES = {"y", "yes", "true", "1", "是", "t", "on"}
_BOOL_NO  = {"n", "no",  "false","0", "否", "f", "off"}


def _fmt_val(key: str, val: Any) -> str:
    if isinstance(val, bool):
        return "✓ 是" if val else "✗ 否"
    s = str(val)
    return "(未设置)" if s.strip() == "" else s


def _show_menu(cfg: Dict[str, Any], config_path: Path) -> None:
    miss = set(missing_fields(cfg))
    # 列宽
    LAB_W = 14   # 标签列显示宽
    VAL_W = _MW - 4 - 3 - LAB_W - 4 - 4  # 剩余给值列

    def hl(t: str) -> str:
        return "─" * t if isinstance(t, int) else t

    top_bar    = "─" * _MW
    inner_bar  = "─" * _MW

    print()
    print(f"┌{top_bar}┐")
    title = "ComfyUI 递归批量处理 · 配置中心"
    td = _dw(title)
    lp = (_MW - td) // 2
    rp = _MW - td - lp
    print(f"│{' ' * lp}{title}{' ' * rp}│")
    print(f"├{inner_bar}┤")

    cf_str = f" 配置文件: {config_path}"
    print(f"│{_pad(cf_str, _MW)}│")

    print(f"├{'─'*4}┬{'─'*(LAB_W+2)}┬{'─'*(VAL_W+2)}┤")
    hdr_no  = _pad(" # ", 4)
    hdr_lab = _pad(" 参数", LAB_W + 2)
    hdr_val = _pad(" 当前值", VAL_W + 2)
    print(f"│{hdr_no}│{hdr_lab}│{hdr_val}│")
    print(f"├{'─'*4}┼{'─'*(LAB_W+2)}┼{'─'*(VAL_W+2)}┤")

    for i, key in enumerate(SCHEMA_KEYS, 1):
        val    = cfg.get(key, _DEFAULT_OF[key])
        label  = _LABEL_OF[key]
        warn   = " ⚠" if key in miss else ""
        disp   = _fmt_val(key, val)
        # 截断过长的值
        while _dw(disp) > VAL_W:
            disp = "…" + disp[1:]

        col_no  = _pad(f" {i} ", 4)
        col_lab = _pad(f" {label}{warn}", LAB_W + 2)
        col_val = _pad(f" {disp}", VAL_W + 2)
        print(f"│{col_no}│{col_lab}│{col_val}│")

    print(f"├{'─'*4}┴{'─'*(LAB_W+2)}┴{'─'*(VAL_W+2)}┤")

    if miss:
        wl = " ⚠ 以下必填项尚未设置: " + "、".join(_LABEL_OF[k] for k in miss)
        print(f"│{_pad(wl, _MW)}│")

    footer = " 输入编号修改  ·  r/回车=开始运行  ·  s=保存  ·  q=退出"
    print(f"│{_pad(footer, _MW)}│")
    print(f"└{'─' * _MW}┘")


def _prompt_field(key: str, cfg: Dict[str, Any]) -> None:
    typ     = _TYPE_OF[key]
    label   = _LABEL_OF[key]
    current = cfg.get(key, _DEFAULT_OF[key])

    if typ is bool:
        cur_hint = "是(y)/否(n)"
        cur_show = "是" if current else "否"
        raw = input(f"\n  [{label}] 当前={cur_show}，输入 {cur_hint}，回车保持: ").strip().lower()
        if raw in _BOOL_YES:
            cfg[key] = True
        elif raw in _BOOL_NO:
            cfg[key] = False
        elif raw != "":
            print("  (无法识别，保持原值)")
    else:
        cur_show = str(current) if str(current).strip() else "(空)"
        raw = input(f"\n  [{label}] 当前={cur_show}\n  新值 (回车保持): ").strip()
        if raw == "":
            return
        try:
            cfg[key] = int(raw) if typ is int else float(raw) if typ is float else raw
        except ValueError:
            print(f"  (格式错误，需要 {typ.__name__}，保持原值)")


def interactive_menu(cfg: Dict[str, Any], config_path: Path) -> Optional[Dict[str, Any]]:
    """
    交互式菜单主循环。
    返回最终确认的 cfg，用户退出则返回 None。
    """
    while True:
        _show_menu(cfg, config_path)
        try:
            raw = input("\n  请输入选项: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        cmd = raw.lower()

        if cmd in ("q", "quit", "exit", "退出"):
            return None

        if cmd in ("s", "save", "保存", "s"):
            save_toml(config_path, cfg)
            print(f"\n  ✓ 已保存到 {config_path}")
            time.sleep(0.6)
            continue

        if cmd in ("r", "run", "", "开始", "运行"):
            miss = missing_fields(cfg)
            if miss:
                labels = "、".join(_LABEL_OF[k] for k in miss)
                print(f"\n  ✗ 必填项未设置: {labels}，请先补充。")
                time.sleep(1.0)
                continue
            # 运行前自动保存
            save_toml(config_path, cfg)
            print(f"\n  ✓ 配置已保存，开始处理…")
            return cfg

        # 数字索引
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(SCHEMA_KEYS):
                _prompt_field(SCHEMA_KEYS[idx - 1], cfg)
                continue

        print("  (无效输入，请重试)")
        time.sleep(0.3)


# ===========================================================================
# 六、ComfyUI 工作流解析
# ===========================================================================

IMAGE_EXTENSIONS: Tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".webp", ".avif",
    ".bmp", ".tif", ".tiff",
)
_SKIP_NODE_TYPES = {"PreviewImage"}


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _link_map(wf: dict) -> Dict[int, list]:
    return {int(lnk[0]): lnk for lnk in wf.get("links", [])}


def _widget_values(node: dict) -> list:
    vals = list(node.get("widgets_values", []))
    # KSampler 在索引 1 处有一个纯 UI 用的 control_after_generate
    if node.get("type") == "KSampler" and len(vals) == 7:
        return [vals[0]] + vals[2:]
    return vals


def workflow_to_prompt(wf: dict) -> Dict[str, dict]:
    lmap   = _link_map(wf)
    prompt: Dict[str, dict] = {}

    for node in wf.get("nodes", []):
        if node.get("type") in _SKIP_NODE_TYPES:
            continue

        nid   = str(node["id"])
        ctype = node["type"]
        ninp: dict = {}

        # 1) 连线输入
        for port in node.get("inputs", []):
            if port.get("type") == "IMAGEUPLOAD" or port.get("name") == "upload":
                continue
            lid = port.get("link")
            if isinstance(lid, int) and lid in lmap:
                lnk = lmap[lid]
                ninp[port["name"]] = [str(lnk[1]), int(lnk[2])]

        # 2) Widget 字面量输入
        wvals  = _widget_values(node)
        widx   = 0
        for port in node.get("inputs", []):
            if port.get("type") == "IMAGEUPLOAD" or port.get("name") == "upload":
                continue
            if port.get("name") in ninp:
                continue
            if "widget" in port:
                if widx < len(wvals):
                    ninp[port["name"]] = wvals[widx]
                widx += 1

        prompt[nid] = {"class_type": ctype, "inputs": ninp}

        # 兼容性补丁
        if ctype == "ImageScaleToMaxDimension":
            if "largest_size" not in ninp:
                ninp["largest_size"] = (
                    ninp.get("max_dimension") or (wvals[-1] if wvals else 1080)
                )
            ninp.setdefault("upscale_method", "lanczos")

    return prompt


def _find_node(wf: dict, ntype: str) -> str:
    for n in wf.get("nodes", []):
        if n.get("type") == ntype:
            return str(n["id"])
    raise ValueError(f"工作流中未找到节点类型: {ntype}")


def _all_nodes_of(wf: dict, ntype: str) -> List[str]:
    return [str(n["id"]) for n in wf.get("nodes", []) if n.get("type") == ntype]


def _best_save_node(wf: dict) -> str:
    ids = _all_nodes_of(wf, "SaveImage")
    if not ids:
        raise ValueError("工作流中没有 SaveImage 节点")
    if len(ids) == 1:
        return ids[0]

    id2n   = {str(n["id"]): n for n in wf.get("nodes", [])}
    lmap   = _link_map(wf)
    prefer = {"VAEDecode", "KSampler", "KSamplerAdvanced",
               "ImageUpscaleWithModel", "UltimateSDUpscale"}

    def score(sid: str) -> Tuple[int, int]:
        node = id2n.get(sid, {})
        ilink = next((p.get("link") for p in (node.get("inputs") or [])
                      if p.get("name") == "images"), None)
        if not isinstance(ilink, int) or ilink not in lmap:
            return (0, int(sid))
        utype = id2n.get(str(lmap[ilink][1]), {}).get("type", "")
        return (3 if utype in prefer else 1 if utype == "LoadImage" else 2, int(sid))

    return max(ids, key=score)


# ===========================================================================
# 七、ComfyUI HTTP API
# ===========================================================================

def _norm_server(s: str) -> str:
    s = s.strip().rstrip("/")
    return s if s.startswith(("http://", "https://")) else "http://" + s


def upload_image(server: str, path: Path) -> str:
    with path.open("rb") as f:
        r = requests.post(
            f"{server}/upload/image",
            files={"image": (path.name, f, "application/octet-stream")},
            data={"type": "input", "overwrite": "true"},
            timeout=120,
        )
    r.raise_for_status()
    p   = r.json()
    sub = p.get("subfolder", "")
    nm  = p.get("name", path.name)
    return f"{sub}/{nm}" if sub else nm


def queue_prompt(server: str, prompt: dict, client_id: str) -> str:
    r = requests.post(f"{server}/prompt",
                      json={"prompt": prompt, "client_id": client_id},
                      timeout=120)
    if r.status_code >= 400:
        try:
            body = json.dumps(r.json(), ensure_ascii=False, indent=2)
        except Exception:
            body = r.text
        raise RuntimeError(f"/prompt HTTP {r.status_code}\n{body}")
    data = r.json()
    if "prompt_id" not in data:
        raise RuntimeError(f"Unexpected /prompt response: {data}")
    return data["prompt_id"]


def wait_history(server: str, prompt_id: str,
                 poll: float = 0.8, timeout: int = 3600) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{server}/history/{prompt_id}", timeout=60)
        r.raise_for_status()
        data = r.json()
        if prompt_id in data:
            return data[prompt_id]
        time.sleep(poll)
    raise TimeoutError(f"等待 prompt {prompt_id} 超时")


def _exec_error(item: dict) -> Optional[str]:
    for msg in (item.get("status") or {}).get("messages") or []:
        if isinstance(msg, list) and len(msg) >= 2 and msg[0] == "execution_error":
            p = msg[1] if isinstance(msg[1], dict) else {}
            return (f"节点 {p.get('node_id','?')} ({p.get('node_type','?')}): "
                    f"{p.get('exception_type','?')} {p.get('exception_message','')}").strip()
    return None


def _output_images(item: dict, preferred: Optional[str] = None) -> List[dict]:
    outputs = item.get("outputs", {})
    if preferred:
        imgs = [i for i in (outputs.get(str(preferred), {}).get("images") or [])
                if isinstance(i, dict) and "filename" in i]
        if imgs:
            return imgs
    return [i for out in outputs.values()
            for i in (out.get("images") or [])
            if isinstance(i, dict) and "filename" in i]


def download_image(server: str, info: dict, dest: Path) -> None:
    r = requests.get(f"{server}/view", params={
        "filename": info.get("filename", ""),
        "subfolder": info.get("subfolder", ""),
        "type": info.get("type", "output"),
    }, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)


# ===========================================================================
# 八、文件发现
# ===========================================================================

def collect_images(root: Path) -> List[Path]:
    """递归收集所有图片，同目录内按文件名排序，目录按名称排序。"""
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if Path(name).suffix.lower() in IMAGE_EXTENSIONS:
                found.append(Path(dirpath) / name)
    return found


# ===========================================================================
# 九、批量处理主逻辑
# ===========================================================================

def run_batch(cfg: Dict[str, Any]) -> None:
    server     = _norm_server(cfg["server"])
    wf_path    = Path(cfg["workflow"]).resolve()
    input_dir  = Path(cfg["input_dir"]).resolve()
    output_dir = Path(cfg["output_dir"]).resolve()

    if not wf_path.exists():
        sys.exit(f"[ERROR] 工作流文件不存在: {wf_path}")
    if not input_dir.is_dir():
        sys.exit(f"[ERROR] 输入目录不存在: {input_dir}")

    wf              = _load_json(wf_path)
    prompt_tmpl     = workflow_to_prompt(wf)
    load_node_id    = _find_node(wf, "LoadImage")
    save_node_id    = _best_save_node(wf)

    images = collect_images(input_dir)
    if not images:
        sys.exit(f"[ERROR] 输入目录中未找到图片: {input_dir}")

    total     = len(images)
    nw        = len(str(total))           # 数字宽度，用于对齐进度
    client_id = str(uuid.uuid4())
    errors:   List[str] = []
    skipped = processed = 0

    print()
    print("=" * _MW)
    print(f"  服务器  : {server}")
    print(f"  工作流  : {wf_path.name}")
    print(f"  输入    : {input_dir}  ({total} 张)")
    print(f"  输出    : {output_dir}")
    print(f"  跳过已存在={cfg['skip_existing']}  "
          f"出错继续={cfg['continue_on_error']}  "
          f"间隔={cfg['sleep']}s  超时={cfg['timeout']}s")
    print("=" * _MW)

    for idx, img_path in enumerate(images, 1):
        rel     = img_path.relative_to(input_dir)
        out_sub = output_dir / rel.parent
        out_sub.mkdir(parents=True, exist_ok=True)
        tag     = f"[{idx:{nw}d}/{total}]"

        # ── 跳过已处理 ──
        if cfg["skip_existing"] and any(out_sub.glob(f"{img_path.stem}*")):
            print(f"{tag} SKIP  {rel}")
            skipped += 1
            continue

        print(f"{tag} {rel}")
        indent = " " * (len(tag) + 1)

        try:
            # 1. 上传源图
            uploaded = upload_image(server, img_path)
            print(f"{indent}↑ uploaded  {uploaded}")

            # 2. 构造 prompt（深拷贝模板）
            prompt = json.loads(json.dumps(prompt_tmpl))
            prompt[load_node_id]["inputs"]["image"]           = uploaded
            prompt[save_node_id]["inputs"]["filename_prefix"] = img_path.stem

            # 3. 提交并等待
            pid     = queue_prompt(server, prompt, client_id)
            history = wait_history(server, pid, timeout=int(cfg["timeout"]))

            # 4. 检查执行错误
            err = _exec_error(history)
            if err:
                raise RuntimeError(err)

            # 5. 下载结果
            out_imgs = _output_images(history, preferred=save_node_id)
            if not out_imgs:
                raise RuntimeError("ComfyUI 未返回任何输出图片")

            for i, info in enumerate(out_imgs, 1):
                suf      = Path(info.get("filename", "out.png")).suffix or ".png"
                out_name = (f"{img_path.stem}{suf}"
                            if len(out_imgs) == 1
                            else f"{img_path.stem}_{i:02d}{suf}")
                dest = out_sub / out_name
                download_image(server, info, dest)
                # 打印相对于 output_dir 父目录的路径，保持简洁
                try:
                    rel_out = dest.relative_to(output_dir.parent)
                except ValueError:
                    rel_out = dest
                print(f"{indent}↓ saved     {rel_out}")

            processed += 1

        except Exception as exc:
            msg = f"[ERROR] {rel}: {exc}"
            if cfg["continue_on_error"]:
                print(msg)
                errors.append(str(rel))
            else:
                raise

        if cfg["sleep"] > 0:
            time.sleep(float(cfg["sleep"]))

    # ── 汇总 ──
    print()
    print("=" * _MW)
    print(f"  完成  ✓{processed} 张  跳过 {skipped} 张  失败 {len(errors)} 张")
    if errors:
        print("  失败列表:")
        for e in errors:
            print(f"    ✗ {e}")
    print("=" * _MW)


# ===========================================================================
# 十、命令行入口
# ===========================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ComfyUI 递归批量处理（TOML 配置 + 交互式菜单）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config",
                   help=f"TOML 配置文件路径（默认与脚本同目录同名 .toml）")
    p.add_argument("--no-interactive", action="store_true",
                   help="跳过交互式菜单，直接按配置运行")
    # 以下参数可覆盖配置文件中的对应项
    p.add_argument("--server",            default="",   help="ComfyUI 服务器地址")
    p.add_argument("--workflow",          default="",   help="工作流 JSON 路径")
    p.add_argument("--input-dir",         default="",   dest="input_dir",          help="输入图片目录")
    p.add_argument("--output-dir",        default="",   dest="output_dir",         help="输出目录")
    p.add_argument("--sleep",             default=None, type=float,                help="任务间隔秒数")
    p.add_argument("--timeout",           default=None, type=int,                  help="单张超时秒数")
    p.add_argument("--skip-existing",     action="store_true", dest="skip_existing",
                   help="跳过输出目录中已存在同名文件的图片")
    p.add_argument("--continue-on-error", action="store_true", dest="continue_on_error",
                   help="遇到错误时记录并继续，而非中断")
    return p


def main() -> None:
    # Windows 控制台 UTF-8 输出
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = _build_parser()
    args   = parser.parse_args()

    # ── 1. 确定配置文件路径 ────────────────────────────────────────────────
    config_path = Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH

    # ── 2. 读取或生成配置 ──────────────────────────────────────────────────
    if config_path.exists():
        cfg = cfg_from_toml(config_path)
    else:
        cfg = default_cfg()
        save_toml(config_path, cfg)
        print(f"\n  ✓ 已生成配置模板: {config_path}")
        print(    "    请填写必填项后重新运行，或通过下方菜单设置。\n")

    # ── 3. 命令行参数覆盖（只有显式传值时才覆盖）─────────────────────────
    _CLI_MAP = [
        ("server",            "server"),
        ("workflow",          "workflow"),
        ("input_dir",         "input_dir"),
        ("output_dir",        "output_dir"),
        ("sleep",             "sleep"),
        ("timeout",           "timeout"),
        ("skip_existing",     "skip_existing"),
        ("continue_on_error", "continue_on_error"),
    ]
    for attr, key in _CLI_MAP:
        val = getattr(args, attr, None)
        # store_true 默认 False，只有用户显式传递时才为 True
        if val is None or val == "" or val is False:
            continue
        cfg[key] = val

    # ── 4. 交互式菜单 ──────────────────────────────────────────────────────
    if args.no_interactive:
        # 无菜单模式：直接校验必填项
        miss = missing_fields(cfg)
        if miss:
            labels = "、".join(_LABEL_OF[k] for k in miss)
            sys.exit(f"[ERROR] 必填项未设置: {labels}")
    else:
        result = interactive_menu(cfg, config_path)
        if result is None:
            print("\n  已退出。")
            return
        cfg = result

    # ── 5. 运行批量处理 ────────────────────────────────────────────────────
    run_batch(cfg)


if __name__ == "__main__":
    main()
