#!/usr/bin/env python
# -*- coding: utf-8 -*-

import copy, json, logging, logging.config, os, sys, threading, time, queue, re, sqlite3
from pathlib import Path
from tkinter import Tk, Toplevel, Frame, LabelFrame, Label, Button, Entry, Text, Scrollbar, messagebox, StringVar, IntVar, BooleanVar, Listbox, filedialog, DISABLED, NORMAL, END, RIGHT, Y, BOTH, LEFT, X, TOP, BOTTOM, WORD, ttk, Spinbox, Checkbutton, EXTENDED, W

SCRIPT_DIR = os.path.split(os.path.realpath(__file__))[0]
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# 将 src/ 加入 sys.path，所有模块已移至 src/ 子目录
_src_dir = os.path.join(SCRIPT_DIR, "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

import const
from weibo import Weibo, get_config as get_weibo_config, handle_config_renaming
from keyword_manager import keyword_mgr
from user_manager import user_mgr

NL = chr(10)

log_dir = os.path.join(SCRIPT_DIR, "log")
os.makedirs(log_dir, exist_ok=True)
log_conf = os.path.join(SCRIPT_DIR, "logging.conf")
if os.path.isfile(log_conf):
    _old = os.getcwd(); os.chdir(SCRIPT_DIR)
    try: logging.config.fileConfig(log_conf)
    finally: os.chdir(_old)
logger = logging.getLogger("gui")
log_queue = queue.Queue()

class QueueHandler(logging.Handler):
    def emit(self, record): log_queue.put(self.format(record))

logging.getLogger().addHandler(QueueHandler())
logging.getLogger("weibo").addHandler(QueueHandler())

class StopCrawlException(Exception): pass


class ToolTip:
    """鼠标悬停时弹出提示标签（400ms 延迟）"""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        self._enter_id = None
        widget.bind('<Enter>', self._schedule)
        widget.bind('<Leave>', self._hide)
        widget.bind('<Button-1>', self._hide)

    def _schedule(self, event=None):
        self._hide()
        self._enter_id = self.widget.after(400, self._show)

    def _show(self):
        self._enter_id = None
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip_window = tw = Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        Label(tw, text=self.text, justify='left',
              background="#ffffe0", relief='solid', borderwidth=1,
              font=("", 9), padx=5, pady=3, wraplength=360).pack()

    def _hide(self, event=None):
        if self._enter_id:
            self.widget.after_cancel(self._enter_id)
            self._enter_id = None
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None

class CrawlerController:
    def __init__(self):
        self.pause_event = threading.Event(); self.pause_event.set()
        self.stop_event = threading.Event()
        self.captcha_event = threading.Event()  # 验证码完成事件
        self.thread = None; self.weibo = None
        self.state = "idle"; self.progress = 0; self.total = 0
        self.captcha_waiting = False  # 是否正在等待验证码
        self.config = None; self.load_config()

    def load_config(self): self.config = get_weibo_config(); return self.config

    def save_config(self, d=None):
        if d: self.config = d
        cfg = os.path.join(SCRIPT_DIR, "config.json")
        with open(cfg, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, ensure_ascii=False, indent=4)

    def start_crawl(self, cb_progress=None, cb_log=None, override=None):
        if self.thread and self.thread.is_alive(): return False
        self.stop_event.clear(); self.pause_event.set()
        self.captcha_event.clear(); self.captcha_waiting = False
        self.state = "running"; self.progress = 0; self.total = 0
        self.thread = threading.Thread(target=self._crawl_worker, args=(cb_progress, override), daemon=True)
        self.thread.start(); return True

    def pause_crawl(self): self.pause_event.clear(); self.state = "paused"
    def resume_crawl(self): self.pause_event.set(); self.state = "running"
    def stop_crawl(self): self.stop_event.set(); self.pause_event.set(); self.state = "stopped"

    def _make_pauseable_sleep(self, orig):
        def ps(t):
            if t <= 0: return
            chunks = max(1, int(t / 0.5))
            for _ in range(chunks):
                if self.stop_event.is_set(): raise StopCrawlException("stop")
                if not self.pause_event.is_set():
                    self.state = "paused"; self.pause_event.wait()
                    if self.stop_event.is_set(): raise StopCrawlException("stop")
                    self.state = "running"
                orig(min(0.5, t))
        return ps

    def _crawl_worker(self, cb_progress, override=None):
        _real = time.sleep; _cwd = os.getcwd()
        try:
            os.chdir(SCRIPT_DIR)
            if not os.path.isdir("./weibo"): os.makedirs("./weibo")
            ps = self._make_pauseable_sleep(time.sleep)
            time.sleep = ps
            import weibo as wm; wm.sleep = ps
            config = override or (self.config or self.load_config())
            wb = Weibo(config); self.weibo = wb
            wb.captcha_event = self.captcha_event  # 验证码事件
            wb.stop_event = self.stop_event        # 停止事件（用于验证码等待时中断）
            self.total = len(wb.user_config_list); self.state = "running"
            if config.get("_test_mode"):
                wb.start_page = 1
                wb.get_page_count = lambda: 1
                # 测试模式：加速 sleep，重置状态避免"没有新微博"
                _orig_sleep_test = time.sleep
                time.sleep = lambda t: _orig_sleep_test(max(0.5, t / 10))
                wb.first_crawler = True
                wb.last_weibo_id = ""
            _oii = wb.initialize_info; _ui = [0]
            _is_test = config.get("_test_mode")
            def _pii(uc):
                _ui[0] += 1; idx = _ui[0] - 1
                self.progress = idx; self.current_user = str(uc.get("user_id", ""))
                if cb_progress: cb_progress(self.current_user, idx, self.total)
                result = _oii(uc)
                if _is_test:
                    wb.first_crawler = True
                    wb.last_weibo_id = ""
                return result
            wb.initialize_info = _pii
            _owd = wb.write_data
            def _pwd(wc): _owd(wc); self._post_write_tasks(wb)
            wb.write_data = _pwd
            wb.start()
            self.progress = self.total; self.state = "finished"
        except StopCrawlException:
            self.state = "stopped"
        except Exception as e:
            logger.exception("爬虫出错"); self.state = "error"
        finally:
            self.captcha_waiting = False   # 重置验证码状态
            self.captcha_event.set()        # 确保验证码等待被解锁
            time.sleep = _real
            import weibo as wm; wm.sleep = _real
            if self.stop_event.is_set() and self.weibo:
                try: self.weibo.write_data(0)
                except: pass
            if self.weibo:
                self._final_health_report(self.weibo)
            os.chdir(_cwd)

    def _post_write_tasks(self, wb):
        try: self._quick_health_check(wb); self._update_index(wb)
        except: pass

    def _final_health_report(self, wb):
        for fn in [self._quick_health_check, self._update_index, self._coverage_report]:
            try: fn(wb)
            except: pass

    def _quick_health_check(self, wb):
        db = os.path.join(SCRIPT_DIR, "weibo", "weibodata.db")
        if not os.path.exists(db): return
        try:
            con = sqlite3.connect(db); cur = con.cursor()
            cur.execute("PRAGMA integrity_check")
            r = cur.fetchone(); con.close()
            if r and r[0] != "ok": logger.warning("数据库异常: %s", r[0])
        except: pass

    def _update_index(self, wb, force=False):
        sn = (wb.user or {}).get("screen_name", "")
        if not sn: return
        od = self.config.get("output_directory", "output") if self.config else "output"
        ud = os.path.join(SCRIPT_DIR, od, sn)
        os.makedirs(ud, exist_ok=True)
        db = os.path.join(SCRIPT_DIR, "weibo", "weibodata.db")
        if not os.path.exists(db): return
        uid = str((wb.user_config or {}).get("user_id", ""))
        con = sqlite3.connect(db); cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM weibo WHERE user_id=?", (uid,))
        total = cur.fetchone()[0]
        cur.execute("SELECT MIN(created_at), MAX(created_at) FROM weibo WHERE user_id=? AND created_at IS NOT NULL", (uid,))
        row = cur.fetchone()
        dr = "{} ~ {}".format(row[0][:10], row[1][:10]) if row and row[0] else "-"
        cur.execute("SELECT substr(created_at,1,7) as m, COUNT(*) FROM weibo WHERE user_id=? AND created_at IS NOT NULL GROUP BY m ORDER BY m DESC", (uid,))
        months = cur.fetchall(); con.close()
        lines = ["# {} - 索引".format(sn), "> 总条数: {} | 范围: {}".format(total, dr), ""]
        for m, c in months: lines.append("| {} | {} |".format(m, c))
        with open(os.path.join(ud, "INDEX.md"), 'w', encoding='utf-8') as f:
            f.write(NL.join(lines) + NL)

    def _coverage_report(self, wb):
        db = os.path.join(SCRIPT_DIR, "weibo", "weibodata.db")
        if not os.path.exists(db): return
        uid = str((wb.user_config or {}).get("user_id", ""))
        con = sqlite3.connect(db); cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM weibo WHERE user_id=?", (uid,))
        total = cur.fetchone()[0]; con.close()
        label = (wb.user or {}).get("screen_name", uid)
        logger.info("数据库 [%s]: %d 条", label, total)

# ═══════════════════ 分割大 MD 文件 ═══════════════════

def _split_large_md(filepath, max_mb=10, log=None):
    """将过大的 .md 文件按 H2 标题边界分割，每个分块 < max_mb MB。
    返回: (是否分割, 生成文件数)"""
    fp = Path(filepath) if not isinstance(filepath, Path) else filepath
    if not fp.exists():
        return False, 0
    raw = fp.read_bytes()
    if len(raw) <= max_mb * 1024 * 1024:
        return False, 0
    text = raw.decode('utf-8', errors='replace')
    lines = text.split('\n')
    h2_idx = [0]
    for i, line in enumerate(lines):
        if re.match(r'^##\s', line):
            h2_idx.append(i)
    h2_idx.append(len(lines))
    sections = []
    for j in range(len(h2_idx) - 1):
        s, e = h2_idx[j], h2_idx[j + 1]
        sections.append((s, e, '\n'.join(lines[s:e])))
    max_bytes = max_mb * 1024 * 1024
    chunks = []
    cur_text = ''
    cur_start = 0
    for s, e, sec_text in sections:
        sec_b = len(sec_text.encode('utf-8'))
        if sec_b > max_bytes:
            if cur_text:
                chunks.append((cur_start, cur_text))
                cur_text = ''
            chunk_lines = []
            cstart = s
            for k in range(s, e):
                lb = len((lines[k] + '\n').encode('utf-8'))
                if chunk_lines and len('\n'.join(chunk_lines + [lines[k]]).encode('utf-8')) > max_bytes:
                    chunks.append((cstart, '\n'.join(chunk_lines)))
                    chunk_lines = [lines[k]]
                    cstart = k
                else:
                    chunk_lines.append(lines[k])
            if chunk_lines:
                chunks.append((cstart, '\n'.join(chunk_lines)))
            cur_start = e
            continue
        test = cur_text + '\n' + sec_text if cur_text else sec_text
        if len(test.encode('utf-8')) > max_bytes and cur_text:
            chunks.append((cur_start, cur_text))
            cur_text = sec_text
            cur_start = s
        else:
            cur_text = test
            if cur_start == 0:
                cur_start = s
    if cur_text:
        chunks.append((cur_start, cur_text))
    if len(chunks) <= 1:
        return False, 0
    stem, ext = fp.stem, fp.suffix
    for ci, (_, chunk_text) in enumerate(chunks, 1):
        new_path = fp.parent / f'{stem}({ci}){ext}'
        new_path.write_text(chunk_text + '\n' if not chunk_text.endswith('\n') else chunk_text, encoding='utf-8')
        if log:
            cmb = len(chunk_text.encode('utf-8')) / (1024 * 1024)
            log(f"    → {new_path.name} ({cmb:.1f} MB)", 'info')
    fp.unlink()
    if log:
        log(f"  ✂ 分割完成: {fp.name} → {len(chunks)} 个文件", 'info')
    return True, len(chunks)


class WeiboCrawlerGUI:
    def __init__(self, root):
        self.root = root; self.root.title("微博爬虫 v2.0 - GUI")
        self.root.state("zoomed"); self.root.minsize(800, 600)
        self.controller = CrawlerController(); self.config = self.controller.config
        self._prev_state = "idle"
        self._create_target_panel(); self._create_user_group_ui(); self._create_config_panel()
        self._create_log_panel(); self._create_status_bar()
        self.poll_interval = 500; self._poll_log_queue(); self._load_crawl_status()
        self._refresh_keyword_ui()  # 初始化关键词分组/最近使用下拉框
        self._refresh_user_group_ui()  # 初始化用户分组下拉框

    def _create_target_panel(self):
        frame = LabelFrame(self.root, text="抓取目标", padx=8, pady=4)
        frame.pack(fill=BOTH, padx=8, pady=4, expand=False)
        self.target_tree = ttk.Treeview(frame, columns=("uid","昵称","日期"),
                                        show="headings", height=6, selectmode=EXTENDED)
        self.target_tree.heading("uid", text="用户ID")
        self.target_tree.heading("昵称", text="昵称")
        self.target_tree.heading("日期", text="爬取日期")
        self.target_tree.column("uid", width=140, anchor=W, minwidth=80)
        self.target_tree.column("昵称", width=120, anchor=W, minwidth=60)
        self.target_tree.column("日期", width=120, anchor=W, minwidth=80)
        self.target_tree.pack(side=LEFT, fill=BOTH, expand=True)
        sb = Scrollbar(frame, command=self.target_tree.yview)
        sb.pack(side=RIGHT, fill=Y); self.target_tree.configure(yscrollcommand=sb.set)
        btn = Frame(self.root); btn.pack(fill=X, padx=8, pady=2)
        b = Button(btn, text="添加", command=self._add_target, width=8); b.pack(side=LEFT, padx=2)
        ToolTip(b, "添加微博用户到抓取列表\n输入用户 ID 和昵称后确认")
        b = Button(btn, text="移除", command=self._remove_target, width=8); b.pack(side=LEFT, padx=2)
        ToolTip(b, "从抓取列表中移除选中的用户")
        self._load_target_list()

    def _load_target_list(self):
        self.target_tree.delete(*self.target_tree.get_children())
        ul = self.config.get("user_id_list", [])
        if isinstance(ul, str) and ul.endswith(".txt"):
            fp = ul if os.path.isabs(ul) else os.path.join(SCRIPT_DIR, ul)
            if os.path.isfile(fp):
                with open(fp, 'r', encoding='utf-8') as f:
                    for line in f:
                        p = line.strip().split(" ", 2)
                        if p and p[0].isdigit():
                            self.target_tree.insert("", END, values=(p[0], p[1] if len(p)>1 else "", ""))

    def _add_target(self):
        dlg = Toplevel(self.root); dlg.title("添加用户"); dlg.geometry("300x150")
        dlg.transient(self.root); dlg.focus_set()
        Label(dlg, text="用户ID:").pack(pady=(12,2))
        uid_var = StringVar(); Entry(dlg, textvariable=uid_var, width=25).pack()
        Label(dlg, text="昵称:").pack(pady=(8,2))
        name_var = StringVar(); Entry(dlg, textvariable=name_var, width=25).pack()
        def ok():
            u = uid_var.get().strip()
            if u.isdigit():
                self.target_tree.insert("", END, values=(u, name_var.get().strip(), ""))
                self._save_targets(silent=True)
            dlg.destroy()
        Button(dlg, text="确定", command=ok).pack(pady=10)

    def _remove_target(self):
        for i in self.target_tree.selection(): self.target_tree.delete(i)
        self._save_targets(silent=True)

    def _save_targets(self, silent=False):
        txt = ""; users_info = []
        for item in self.target_tree.get_children():
            v = self.target_tree.item(item, "values")
            txt += "{} {}\n".format(v[0], v[1])
            users_info.append({"user_id": v[0], "nickname": v[1]})
        out = os.path.join(SCRIPT_DIR, "user_id_list.txt")
        with open(out, 'w', encoding='utf-8') as f: f.write(txt)
        self.config["user_id_list"] = out
        self.controller.save_config(self.config)
        # 记录最近用户组合
        if users_info and not silent:
            display = ", ".join(u["nickname"] or u["user_id"] for u in users_info[:10])
            if len(users_info) > 10: display += f" …共{len(users_info)}人"
            user_mgr.add_recent(display)
            self._refresh_user_group_ui()
        if not silent: messagebox.showinfo("提示", "已保存到 {}".format(out))

    def _create_user_group_ui(self):
        """创建用户分组管理 UI（分组下拉框 + 按钮）"""
        ug_frame = Frame(self.root); ug_frame.pack(fill=X, padx=8, pady=1)
        Label(ug_frame, text="👥 用户分组:", font=("", 8)).pack(side=LEFT, padx=(0, 4))
        self._user_group_var = StringVar()
        self._user_group_combo = ttk.Combobox(ug_frame, textvariable=self._user_group_var,
                                              width=14, state="readonly", font=("", 8))
        self._user_group_combo.pack(side=LEFT, padx=2)
        self._user_group_combo.bind("<<ComboboxSelected>>", self._on_user_group_selected)
        ToolTip(self._user_group_combo, "选择预设的用户组，可一键批量选中")
        Button(ug_frame, text="应用分组", command=self._apply_user_group,
               font=("", 8), width=8).pack(side=LEFT, padx=2)
        Button(ug_frame, text="保存为分组…", command=self._save_as_user_group,
               font=("", 8), width=9).pack(side=LEFT, padx=2)
        Button(ug_frame, text="管理分组…", command=self._manage_user_groups,
               font=("", 8), width=8).pack(side=LEFT, padx=2)
        Button(ug_frame, text="导入文件…", command=self._import_users_file,
               font=("", 8), width=8).pack(side=LEFT, padx=2)
        # 最近使用用户组合
        Label(ug_frame, text="🕐", font=("", 8)).pack(side=RIGHT, padx=(4, 0))
        self._user_recent_var = StringVar()
        self._user_recent_combo = ttk.Combobox(ug_frame, textvariable=self._user_recent_var,
                                               width=26, state="readonly", font=("", 8))
        self._user_recent_combo.pack(side=RIGHT, padx=2)
        self._user_recent_combo.bind("<<ComboboxSelected>>", self._on_recent_users_selected)
        ToolTip(self._user_recent_combo, "最近使用过的用户组合\n点击即可快速恢复之前抓取过的用户列表")

    def _create_config_panel(self):
        frame = LabelFrame(self.root, text="抓取配置", padx=8, pady=4)
        frame.pack(fill=X, padx=8, pady=4)
        r1 = Frame(frame); r1.pack(fill=X, pady=2)
        Label(r1, text="模式:").pack(side=LEFT, padx=(0,2))
        self.mode_var = StringVar(value=const.MODE)
        b = ttk.Combobox(r1, textvariable=self.mode_var, values=["append","overwrite"], state="readonly", width=10)
        b.pack(side=LEFT, padx=2)
        ToolTip(b, "append = 追加模式，从上次中断处继续\noverwrite = 覆盖模式，从头重新抓取")
        Label(r1, text="每页:").pack(side=LEFT, padx=(12,2))
        self.ppc_var = IntVar(value=self.config.get("page_weibo_count", 10))
        b = Spinbox(r1, from_=1, to=50, textvariable=self.ppc_var, width=5)
        b.pack(side=LEFT, padx=2)
        ToolTip(b, "每页抓取的微博条数\n范围 1-50 条\n建议设 10-20 条以免页面加载过慢")
        Label(r1, text="Cookie:").pack(side=LEFT, padx=(12,2))
        self.cookie_var = StringVar(value=self.config.get("cookie", ""))
        self.cookie_label = Label(r1, text=self._mask_cookie(), font=("", 9),
                                   fg="#333", bg="#f5f5f5", width=28, anchor="w", relief="sunken")
        self.cookie_label.pack(side=LEFT, padx=2)
        self.cookie_label.bind("<Button-1>", lambda e: self._edit_cookie())
        Button(r1, text="编辑", command=self._edit_cookie, width=4, font=("", 8)).pack(side=LEFT, padx=(0,2))
        ## Cookie edit tooltip applied to cookie_label (same function)
        ToolTip(self.cookie_label, "点击这里或「编辑」按钮\n粘贴浏览器 Cookie 字符串（登录微博后从开发者工具复制）")
        r2 = Frame(frame); r2.pack(fill=X, pady=2)
        self.orig_only_var = BooleanVar(value=bool(self.config.get("only_crawl_original", 0)))
        cb = Checkbutton(r2, text="仅原创", variable=self.orig_only_var)
        cb.pack(side=LEFT, padx=4)
        ToolTip(cb, "仅抓取原创微博\n不抓取转发内容")
        self.pic_dl_var = BooleanVar(value=bool(self.config.get("original_pic_download", 1)))
        cb = Checkbutton(r2, text="图片", variable=self.pic_dl_var)
        cb.pack(side=LEFT, padx=4)
        ToolTip(cb, "下载微博中的图片到本地\n保存为 base64 嵌入 Markdown")
        self.video_dl_var = BooleanVar(value=bool(self.config.get("original_video_download", 1)))
        cb = Checkbutton(r2, text="视频", variable=self.video_dl_var)
        cb.pack(side=LEFT, padx=4)
        ToolTip(cb, "下载微博中的视频链接\n注：视频文件较大，可能影响爬取速度")
        self.comment_dl_var = BooleanVar(value=bool(self.config.get("download_comment", 1)))
        cb = Checkbutton(r2, text="评论", variable=self.comment_dl_var)
        cb.pack(side=LEFT, padx=4)
        ToolTip(cb, "抓取微博的热门评论\n保存到 Markdown 中")
        self.repost_dl_var = BooleanVar(value=bool(self.config.get("download_repost", 1)))
        cb = Checkbutton(r2, text="转发", variable=self.repost_dl_var)
        cb.pack(side=LEFT, padx=4)
        ToolTip(cb, "抓取微博的转发数据\n跨页抓取全部转发内容")
        ab = self.config.get("anti_ban_config", {})
        self.anti_ban_var = BooleanVar(value=ab.get("enabled", True) if isinstance(ab, dict) else True)
        cb = Checkbutton(r2, text="防封禁", variable=self.anti_ban_var)
        cb.pack(side=LEFT, padx=4)
        ToolTip(cb, "启用随机延迟和反爬策略\n降低被封禁风险\n建议始终开启")
        r3 = Frame(frame); r3.pack(fill=X, pady=2)
        Label(r3, text="时间:").pack(side=LEFT, padx=(0,2))
        self.sd_var = StringVar(value=self.config.get("since_date") or "2024-12-20")
        b = Entry(r3, textvariable=self.sd_var, width=10, font=("", 9))
        b.pack(side=LEFT, padx=2)
        ToolTip(b, "起始日期（包含当天）\n格式: YYYY-MM-DD\n如 2024-12-20")
        Label(r3, text="至").pack(side=LEFT, padx=2)
        self.ed_var = StringVar(value=self.config.get("end_date") or "")
        b = Entry(r3, textvariable=self.ed_var, width=10, font=("", 9))
        b.pack(side=LEFT, padx=2)
        ToolTip(b, "结束日期（包含当天）\n格式: YYYY-MM-DD\n留空 = 不限结束日期\n如 2025-06-20")
        self.all_range_var = BooleanVar(value=False)
        cb = Checkbutton(r3, text="全时段", variable=self.all_range_var, command=self._toggle_range)
        cb.pack(side=LEFT, padx=8)
        ToolTip(cb, "不限制日期范围\n从最早微博开始抓取")
        # 关键词过滤
        kw_filter = self.config.get("keyword_filter", {})
        if not isinstance(kw_filter, dict):
            kw_filter = {}
        self.kw_enabled_var = BooleanVar(value=kw_filter.get("enabled", False))
        cb_kw = Checkbutton(r3, text="🔍 关键词:", variable=self.kw_enabled_var, command=self._toggle_keyword)
        cb_kw.pack(side=LEFT, padx=(20, 2))
        ToolTip(cb_kw, "开启/关闭关键词过滤\n关闭时爬取全部微博，开启时仅保存命中关键词的微博")
        self.kw_var = StringVar(value=kw_filter.get("keyword", ""))
        self.kw_entry = Entry(r3, textvariable=self.kw_var, width=14, font=("", 9))
        self.kw_entry.pack(side=LEFT, padx=2)
        self.kw_entry.bind("<Return>", self._on_keyword_enter)
        ToolTip(self.kw_entry, "多关键词用逗号、顿号、空格、分号等分隔\n如: AI, 人工智能、NLP\n正文或话题标签含任一关键词即保存\n回车确认并记录到最近使用")
        self._toggle_keyword()
        # ── 关键词分组管理 ──
        r3b = Frame(self.root); r3b.pack(fill=X, padx=8, pady=1)
        Label(r3b, text="📂 分组:", font=("", 8)).pack(side=LEFT, padx=(20, 4))
        self._group_var = StringVar()
        self._group_combo = ttk.Combobox(r3b, textvariable=self._group_var,
                                         width=12, state="readonly", font=("", 8))
        self._group_combo.pack(side=LEFT, padx=2)
        self._group_combo.bind("<<ComboboxSelected>>", self._on_group_selected)
        ToolTip(self._group_combo, "选择预设的关键词组，可一键填入\n先在下方管理分组中创建关键词组")
        Button(r3b, text="应用分组", command=self._apply_group,
               font=("", 8), width=8).pack(side=LEFT, padx=2)
        Button(r3b, text="保存为分组…", command=self._save_as_group,
               font=("", 8), width=9).pack(side=LEFT, padx=2)
        Button(r3b, text="管理分组…", command=self._manage_groups,
               font=("", 8), width=8).pack(side=LEFT, padx=2)
        Button(r3b, text="导入文件…", command=self._import_keywords_file,
               font=("", 8), width=8).pack(side=LEFT, padx=2)
        # ── 最近使用关键词 ──
        r3c = Frame(self.root); r3c.pack(fill=X, padx=8, pady=1)
        Label(r3c, text="🕐 最近:", font=("", 8)).pack(side=LEFT, padx=(20, 4))
        self._recent_var = StringVar()
        self._recent_combo = ttk.Combobox(r3c, textvariable=self._recent_var,
                                          width=50, state="readonly", font=("", 8))
        self._recent_combo.pack(side=LEFT, padx=2, fill=X, expand=True)
        self._recent_combo.bind("<<ComboboxSelected>>", self._on_recent_selected)
        ToolTip(self._recent_combo, "最近使用过的关键词\n点击即可快速填入输入框")
        self._toggle_range()
        # ── 手动链接保存 ──
        manual_frame = Frame(self.root); manual_frame.pack(fill=X, padx=8, pady=(6, 2))
        Label(manual_frame, text="🔗 手动链接 (每行一条 · 上限100 · 支持导入文件分批):", font=("", 9)).pack(anchor="w")
        self._manual_text = Text(manual_frame, height=4, font=("Consolas", 9),
                                  wrap=WORD, fg="gray")
        self._manual_text.insert("1.0", "粘贴微博链接，每行一条...\n如 https://weibo.com/xxx/xxx")
        self._manual_text.bind('<FocusIn>', self._on_manual_focus_in)
        self._manual_text.bind('<FocusOut>', self._on_manual_focus_out)
        self._manual_text.pack(fill=X, pady=(2, 0))
        ToolTip(self._manual_text, "粘贴微博链接，每行一条\n支持 weibo.com / m.weibo.cn\n上限 100 条，超出可用导入文件分批处理")
        btn_row2 = Frame(manual_frame); btn_row2.pack(fill=X, pady=(4, 0))
        self._manual_btn = Button(btn_row2, text="📝 批量保存为 MD", command=self._save_manual_weibos,
                                   bg="#607D8B", fg="white", width=16)
        self._manual_btn.pack(side=LEFT)
        self._import_btn = Button(btn_row2, text="📂 导入文件", command=self._import_link_file,
                                   bg="#607D8B", fg="white", width=12)
        self._import_btn.pack(side=RIGHT)
        ToolTip(self._import_btn, "从 .txt/.csv 文件导入微博链接\n自动提取链接并去重\n超 100 条自动分批显示")
        ToolTip(self._manual_btn, "将上方粘贴的微博链接逐条抓取\n保存为 Markdown 文件\n支持 weibo.com / m.weibo.cn 链接\n最多同时处理 100 条")
        btn = Frame(self.root); btn.pack(fill=X, padx=8, pady=2)
        self.start_btn = Button(btn, text="开始抓取", command=self._start, bg="#4CAF50", fg="white", width=12)
        self.start_btn.pack(side=LEFT, padx=4)
        ToolTip(self.start_btn, "开始爬取所有用户的微博\n若选中某个用户，可选择仅爬取该用户")
        self.pause_btn = Button(btn, text="暂停", command=self._pause, state=DISABLED, width=8)
        self.pause_btn.pack(side=LEFT, padx=4)
        ToolTip(self.pause_btn, "暂停当前爬取任务（可稍后恢复）")
        self.resume_btn = Button(btn, text="继续", command=self._resume, state=DISABLED, width=8)
        self.resume_btn.pack(side=LEFT, padx=4)
        ToolTip(self.resume_btn, "恢复已暂停的爬取任务")
        self.stop_btn = Button(btn, text="停止", command=self._stop, state=DISABLED, bg="#f44336", fg="white", width=8)
        self.stop_btn.pack(side=LEFT, padx=4)
        ToolTip(self.stop_btn, "停止当前爬取任务\n已爬取的数据会被保存")
        b = Button(btn, text="测试(3条)", command=self._start_test, width=10); b.pack(side=LEFT, padx=4)
        ToolTip(b, "快速测试模式：仅爬取选中用户的最新 3 条\n加速运行（忽略反爬延迟）\n需要先选择目标用户")
        b = Button(btn, text="✂ 分割大MD", command=self._split_large_md_file, bg="#607D8B", fg="white", width=12); b.pack(side=LEFT, padx=4)
        ToolTip(b, "扫描 output 目录中超过 10MB 的 Markdown 文件\n按二级标题（## ）自动分割为小文件\n分块命名：原文件名(1).md / (2).md ...")
        self.target_tree.bind("<<TreeviewSelect>>", lambda e: self._on_select_user())

    def _start(self):
        sel = self.target_tree.selection(); self._save_config(); self._save_targets()
        override = None
        if sel:
            v = self.target_tree.item(sel[0], "values")
            uid, name = v[0], v[1]
            if messagebox.askyesno("选择性抓取", "仅抓取 {}({})？".format(name, uid)):
                override = self.config.copy(); override["user_id_list"] = [uid]

        # ── 关键词确认弹窗 ──
        from weibo import _split_keywords
        kf = self.config.get("keyword_filter", {})
        kw_enabled = kf.get("enabled", False)
        kw_str = kf.get("keyword", "").strip()
        kws = _split_keywords(kw_str) if kw_enabled and kw_str else []
        if kws:
            filter_info = f"🔍 关键词筛选：{len(kws)}个 → {', '.join(kws[:8])}"
            if len(kws) > 8:
                filter_info += f" …(共{len(kws)}个)"
        else:
            filter_info = "⚠ 未启用关键词筛选，将抓取全部微博"
        uid_list = override.get("user_id_list", self.config.get("user_id_list", [])) if override else self.config.get("user_id_list", [])
        confirm_msg = (
            f"确认开始爬取？\n\n"
            f"👤 目标用户: {len(uid_list)}人\n"
            f"{filter_info}"
        )
        if not messagebox.askyesno("确认爬取", confirm_msg, parent=self.root):
            self._append_log("⚠ 用户取消爬取\n")
            return

        # 确认后才记录关键词到最近使用
        if kw_enabled and kw_str:
            keyword_mgr.add_recent(kw_str)
            self._refresh_keyword_ui()

        self.log_text.delete(1.0, END)
        if self.controller.start_crawl(cb_progress=self._on_progress, override=override):
            self._update_button_states()

    def _start_test(self):
        try:
            sel = self.target_tree.selection()
            if not sel: messagebox.showwarning("提示", "请先选择目标"); return
            v = self.target_tree.item(sel[0], "values"); uid, name = v[0], v[1]
            self._save_config()
            tc = self.config.copy()
            tc["user_id_list"] = [uid]
            tc["page_weibo_count"] = 3
            tc["_test_mode"] = True
            tc["end_date"] = ""
            # 覆盖: 无日期限制; 追加: 从2026-01-01开始（_pii会自动更新为SQLite续传时间）
            tc["since_date"] = "2026-01-01" if const.MODE == "append" else "2001-01-01"
            ab = dict(tc.get("anti_ban_config", {}))
            ab["enabled"] = True
            tc["anti_ban_config"] = ab
            for k in ["only_crawl_original","original_pic_download","original_video_download",
                      "download_comment","download_repost"]:
                tc[k] = 1 if tc.get(k) else 0
            self.log_text.delete(1.0, END)
            self._append_log("[测试] {} ({}) - 最新3条\n".format(name, uid))
            if self.controller.start_crawl(cb_progress=self._on_progress, override=tc):
                self._update_button_states()
        except Exception as e:
            self._append_log("[错误] _start_test: {}\n".format(e))
            import traceback
            self._append_log(traceback.format_exc())

    def _pause(self): self.controller.pause_crawl(); self._update_button_states()
    def _resume(self): self.controller.resume_crawl(); self._update_button_states()
    def _stop(self):
        if self.controller.state in ("running","paused"):
            if messagebox.askyesno("确认", "确定停止吗？"):
                self.controller.stop_crawl()
        self._update_button_states()  # 无论什么状态都刷新按钮

    def _verify_done(self):
        """用户点击'验证完成'按钮"""
        self.controller.captcha_event.set()
        self.controller.captcha_waiting = False
        self._update_button_states()
        self._append_log("[用户] 验证完成，继续爬取...\n")

    def _on_progress(self, uid, cur, total):
        self.root.after(0, lambda: self._update_progress_ui(uid, cur, total))

    def _update_progress_ui(self, uid, cur, total):
        self.status_var.set("{}: {}/{}".format(uid, cur, total)); self._update_button_states()

    def _update_button_states(self):
        s = self.controller.state
        self.start_btn.config(state=NORMAL if s in ("idle","finished","stopped","error") else DISABLED)
        self.pause_btn.config(state=NORMAL if s == "running" else DISABLED)
        self.resume_btn.config(state=NORMAL if s == "paused" else DISABLED)
        self.stop_btn.config(state=NORMAL if s in ("running","paused") else DISABLED)

    def _mask_cookie(self):
        c = self.cookie_var.get().strip()
        if not c: return "(未设置)"
        if len(c) <= 12: return c[:4] + "****"
        return "Cookie({}字) 前:{} / 后:{}".format(len(c), c[:15], c[-15:])

    def _edit_cookie(self):
        dlg = Toplevel(self.root); dlg.title("编辑 Cookie"); dlg.geometry("600x200")
        dlg.transient(self.root); dlg.focus_set()
        Label(dlg, text="粘贴完整的 Cookie 字符串:").pack(pady=(12,4))
        ev = StringVar(value=self.cookie_var.get())
        Entry(dlg, textvariable=ev, width=70, font=("", 9)).pack(padx=12, fill=X)
        def save():
            self.cookie_var.set(ev.get().strip())
            self.cookie_label.config(text=self._mask_cookie())
            dlg.destroy()
        Button(dlg, text="保存", command=save).pack(pady=8)

    def _toggle_range(self):
        if self.all_range_var.get():
            self.sd_var.set("2001-01-01")
            self.ed_var.set("")
        else:
            self.sd_var.set(self.config.get("since_date") or "2024-12-20")
            self.ed_var.set(self.config.get("end_date") or "")

    def _toggle_keyword(self):
        if self.kw_enabled_var.get():
            self.kw_entry.config(state=NORMAL)
        else:
            self.kw_entry.config(state=DISABLED)

    def _save_config(self):
        self.config["mode"] = self.mode_var.get()
        self.config["page_weibo_count"] = self.ppc_var.get()
        if self.cookie_var.get(): self.config["cookie"] = self.cookie_var.get()
        self.config["since_date"] = self.sd_var.get()
        self.config["end_date"] = self.ed_var.get()
        self.config["only_crawl_original"] = 1 if self.orig_only_var.get() else 0
        self.config["original_pic_download"] = 1 if self.pic_dl_var.get() else 0
        self.config["original_video_download"] = 1 if self.video_dl_var.get() else 0
        self.config["download_comment"] = 1 if self.comment_dl_var.get() else 0
        self.config["download_repost"] = 1 if self.repost_dl_var.get() else 0
        ab = self.config.get("anti_ban_config", {})
        if not isinstance(ab, dict): ab = {}
        ab["enabled"] = self.anti_ban_var.get()
        self.config["anti_ban_config"] = ab
        self.config["keyword_filter"] = {
            "enabled": self.kw_enabled_var.get(),
            "keyword": self.kw_var.get().strip(),
        }
        # 只保留 sqlite + markdown，不生成 csv/json
        self.config["write_mode"] = ["sqlite", "markdown"]
        const.MODE = self.mode_var.get()  # 同步模块级变量，确保 weibo.py 使用正确模式
        self.controller.save_config(self.config)
        # 注意：add_recent 移到 _start() 确认弹窗之后，避免用户取消爬取时仍记录

    def _on_select_user(self):
        """选中用户时显示简要状态"""
        sel = self.target_tree.selection()
        if not sel: return
        vals = self.target_tree.item(sel[0], "values")
        uid = vals[0]
        db = os.path.join(SCRIPT_DIR, "weibo", "weibodata.db")
        if not os.path.exists(db): return
        try:
            con = sqlite3.connect(db); cur = con.cursor()
            cur.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM weibo WHERE user_id=?", (uid,))
            row = cur.fetchone(); con.close()
            if row and row[0]:
                dr = "{} ~ {}".format(row[1][:10], row[2][:10]) if row[1] else "-"
                self.status_var.set("{}: {} 条, {}".format(vals[1] or uid, row[0], dr))
        except: pass

    def _split_large_md_file(self):
        """选择目录，扫描并分割 >10MB 的 .md 文件"""
        od = self.config.get("output_directory", "output") if self.config else "output"
        d = filedialog.askdirectory(title="选择要扫描的 MD 文件目录",
            initialdir=os.path.join(SCRIPT_DIR, od))
        if not d:
            return
        big_files = []
        for mf in sorted(Path(d).rglob('*.md')):
            if not mf.is_file():
                continue
            sz = mf.stat().st_size
            if sz > 10 * 1024 * 1024:
                big_files.append((str(mf), sz))
        if not big_files:
            messagebox.showinfo("无需分割", "该目录下没有超过 10 MB 的 .md 文件。")
            return
        names = '\n'.join(f'  · {Path(f).name} ({s / 1024 / 1024:.1f} MB)' for f, s in big_files[:20])
        if len(big_files) > 20:
            names += f'\n  ... 共 {len(big_files)} 个'
        ok = messagebox.askyesno(
            "分割大文件",
            f"检测到 {len(big_files)} 个 .md 文件超过 10 MB：\n\n"
            f"{names}\n\n"
            f"是否按 H2 标题自动分割为 <10 MB 的小文件？\n"
            f"分割后编号保留，仅加 (1)(2)(3) 后缀。"
        )
        if not ok:
            return
        self._append_log(f"\n✂ 开始分割大文件...\n")

        def worker():
            count = 0
            for fp, sz in big_files:
                try:
                    did, n = _split_large_md(fp, 10, lambda m, t='info': self.root.after(0, lambda: self._append_log(m + '\n')))
                    if did:
                        count += 1
                except Exception as e:
                    self.root.after(0, lambda e=e, fp=fp: self._append_log(f"  ✗ 分割失败 {Path(fp).name}: {e}\n"))
            self.root.after(0, lambda: self._append_log(f"✂ 分割完成: {len(big_files)} 个大文件 → {count} 个已分割\n"))
            self.root.after(0, lambda c=count: messagebox.showinfo("分割完成", f"已处理 {len(big_files)} 个大文件，\n{count} 个文件已被分割。"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_manual_focus_in(self, event):
        text = self._manual_text.get("1.0", END).strip()
        if text.startswith("粘贴微博链接"):
            self._manual_text.delete("1.0", END)
            self._manual_text.config(fg="black")

    def _on_manual_focus_out(self, event):
        if not self._manual_text.get("1.0", END).strip():
            self._manual_text.insert("1.0", "粘贴微博链接，每行一条...\n如 https://weibo.com/xxx/xxx")
            self._manual_text.config(fg="gray")

    MAX_MANUAL_URLS = 100
    BATCH_SIZE = 100

    def _import_link_file(self):
        """导入链接文件（.txt / .csv），支持超大批次自动分批"""
        fp = filedialog.askopenfilename(
            title="选择链接文件",
            filetypes=[("文本文件", "*.txt"), ("CSV 文件", "*.csv"), ("所有文件", "*.*")]
        )
        if not fp:
            return

        try:
            with open(fp, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(fp, 'r', encoding='gbk') as f:
                content = f.read()

        # 提取微博链接
        urls = re.findall(r'https?://[^\s]*(?:weibo\.com|weibo\.cn)[^\s]*', content)
        # 去重保持顺序
        seen = set()
        urls = [u for u in urls if not (u in seen or seen.add(u))]

        if not urls:
            messagebox.showwarning("提示", f"文件中未找到微博链接\n文件: {Path(fp).name}")
            return

        self._imported_urls = urls

        batches = (len(urls) + self.BATCH_SIZE - 1) // self.BATCH_SIZE
        if len(urls) <= self.MAX_MANUAL_URLS:
            self._manual_text.delete("1.0", END)
            self._manual_text.insert("1.0", "\n".join(urls))
            self._manual_text.config(fg="black")
            msg = f"已导入 {len(urls)} 条链接"
        else:
            self._manual_text.delete("1.0", END)
            self._manual_text.insert(
                "1.0",
                f"已导入 {len(urls)} 条链接（将自动分 {batches} 批处理，每批 {self.BATCH_SIZE} 条）\n"
                f"来源: {Path(fp).name}"
            )
            self._manual_text.config(fg="black")
            msg = f"已导入 {len(urls)} 条链接（分 {batches} 批）"
        self._append_log(msg + "\n")

    def _save_manual_weibos(self, event=None):
        """批量手动微博链接 → 保存为 MD（含导入文件分批支持）"""
        # 优先使用导入的链接列表
        if getattr(self, '_imported_urls', None):
            urls = self._imported_urls
        else:
            raw = self._manual_text.get("1.0", END).strip()
            if not raw or raw.startswith("粘贴微博链接") or raw.startswith("已导入"):
                messagebox.showwarning("提示", "请先粘贴微博链接或导入链接文件")
                return

            urls = [u.strip() for u in raw.splitlines() if u.strip()]
            urls = [u for u in urls if 'weibo.com' in u or 'weibo.cn' in u]

        if not urls:
            messagebox.showwarning("提示", "请粘贴微博链接（weibo.com 或 m.weibo.cn）或导入链接文件")
            return

        # 手动粘贴时限制上限 100
        if not getattr(self, '_imported_urls', None) and len(urls) > self.MAX_MANUAL_URLS:
            urls = urls[:self.MAX_MANUAL_URLS]
            self._manual_text.delete("1.0", END)
            self._manual_text.insert("1.0", "\n".join(urls))

        # 超大批次确认
        if len(urls) > self.BATCH_SIZE:
            batches = (len(urls) + self.BATCH_SIZE - 1) // self.BATCH_SIZE
            ok = messagebox.askyesno(
                "分批处理确认",
                f"共 {len(urls)} 条链接，超过单批上限（{self.BATCH_SIZE}条）。\n\n"
                f"将自动分 {batches} 批处理，是否继续？"
            )
            if not ok:
                return

        if self.controller.state in ("running", "paused"):
            messagebox.showwarning("提示", "已有爬取任务在运行中，请等待完成")
            return

        self._manual_btn.config(state=DISABLED, text=f"保存中 (0/{len(urls)})...")
        self._import_btn.config(state=DISABLED)
        self._save_config()

        def worker():
            success = 0
            fail = 0
            total = len(urls)
            try:
                import weibo as wm
                config = self.controller.config
                wb = Weibo(config)
                batch_count = (total + self.BATCH_SIZE - 1) // self.BATCH_SIZE

                self.root.after(0, lambda: self._append_log(f"\n🔗 批量保存 {total} 条微博链接\n"))
                if batch_count > 1:
                    self.root.after(0, lambda c=batch_count: self._append_log(
                        f"📦 自动分批: 共 {c} 批，每批 {self.BATCH_SIZE} 条\n"))

                for batch_idx in range(batch_count):
                    start = batch_idx * self.BATCH_SIZE
                    end = min(start + self.BATCH_SIZE, total)
                    batch_urls = urls[start:end]

                    if batch_count > 1:
                        self.root.after(0, lambda bi=batch_idx+1, bc=batch_count, s=start+1, e=end:
                            self._append_log(f"\n── 📦 批次 {bi}/{bc} ({s}-{e}) ──\n"))

                    for i, url in enumerate(batch_urls):
                        idx = start + i + 1
                        self.root.after(0, lambda c=idx, t=total: self._manual_btn.config(
                            state=DISABLED, text=f"保存中 ({c}/{t})..."))
                        self.root.after(0, lambda u=url, ii=idx, tt=total:
                            self._append_log(f"  [{ii}/{tt}] {u[:80]}...\n"))
                        result = wb.crawl_single_weibo_url(url)
                        if result['success']:
                            self.root.after(0, lambda p=result['md_path']:
                                self._append_log(f"    ✅ {p}\n"))
                            success += 1
                        else:
                            self.root.after(0, lambda e=result['error']:
                                self._append_log(f"    ❌ {e}\n"))
                            fail += 1
            except Exception as e:
                import traceback
                self.root.after(0, lambda: self._append_log(f"保存异常: {e}\n{traceback.format_exc()}\n"))
            finally:
                self._imported_urls = None  # 清除导入缓存
                summary = f"✅ {success} 成功"
                if fail:
                    summary += f" / ❌ {fail} 失败"
                self.root.after(0, lambda s=summary: self._append_log(f"\n📊 批量保存完成: {s}\n"))
                self.root.after(0, lambda s=summary, t=total: messagebox.showinfo(
                    "批量保存完成", f"批量保存 {t} 条完成:\n{s}"))
                self.root.after(0, lambda: self._manual_btn.config(state=NORMAL, text="📝 批量保存为 MD"))
                self.root.after(0, lambda: self._import_btn.config(state=NORMAL))

        threading.Thread(target=worker, daemon=True).start()

    def _create_log_panel(self):
        frame = LabelFrame(self.root, text="运行日志", padx=8, pady=4)
        frame.pack(fill=BOTH, expand=True, padx=8, pady=4)
        self.log_text = Text(frame, wrap="word", height=6, state=DISABLED, font=("Consolas", 9))
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        Scrollbar(frame, command=self.log_text.yview).pack(side=RIGHT, fill=Y)

    def _append_log(self, msg):
        self.log_text.configure(state=NORMAL)
        self.log_text.insert(END, msg); self.log_text.see(END)
        self.log_text.configure(state=DISABLED)

    def _poll_log_queue(self):
        try:
            while True:
                msg = log_queue.get_nowait()
                self._append_log(msg + NL)
                # 检测验证码提示，自动弹窗提醒用户
                if "等待用户在 GUI 中点击" in msg:
                    self.controller.captcha_waiting = True
                    self._update_button_states()
                    self.root.after(200, self._show_captcha_prompt)
        except: pass
        # 每轮都更新按钮状态，确保线程结束（finished/stopped/error）后按钮及时恢复
        self._update_button_states()
        # 检测状态转为 finished → 自动刷新已抓取用户列表
        cur = self.controller.state
        if cur == "finished" and self._prev_state != "finished":
            self._refresh_crawled_users_report(silent=True)
        self._prev_state = cur
        self.root.after(self.poll_interval, self._poll_log_queue)

    def _show_captcha_prompt(self):
        """弹出验证码提示框"""
        if not self.controller.captcha_waiting:
            return  # 可能已经被处理了
        result = messagebox.askquestion("验证码", 
            "检测到需要验证码！\n\n"
            "1. 浏览器已自动打开验证页面\n"
            "2. 请在浏览器中完成验证（滑动/点击）\n"
            "3. 完成后点击下方'是'按钮继续\n\n"
            "点击'否'将等待下次提醒（或按■停止退出）")
        if result == 'yes':
            self._verify_done()
        # 如果点'否'，按钮仍保持可用状态

    def _create_status_bar(self):
        self.status_var = StringVar(value="就绪")
        Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w", font=("",8)).pack(side=BOTTOM, fill=X)

    def _load_crawl_status(self):
        db = os.path.join(SCRIPT_DIR, "weibo", "weibodata.db")
        if not os.path.exists(db): return
        try:
            con = sqlite3.connect(db); cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='weibo'")
            if not cur.fetchone(): con.close(); return
            cur.execute("SELECT user_id, MAX(created_at), screen_name FROM weibo WHERE created_at IS NOT NULL GROUP BY user_id")
            rows = cur.fetchall(); con.close()
            if not rows: return
            self._append_log("---- 上次状态 ----" + NL)
            for uid, lt, sn in rows:
                self._append_log("  {}({}): {}\n".format(sn, uid, lt))
            if const.MODE == "append":
                self._append_log("-- 将从此时间继续 --" + NL)
            else:
                self._append_log("-- 覆盖模式，将从头抓取 --" + NL)
        except: pass

    # ── 用户分组管理 ────────────────────────────────────

    def _refresh_user_group_ui(self):
        """刷新用户分组下拉框和最近使用下拉框"""
        groups = user_mgr.group_names()
        self._user_group_combo["values"] = groups if groups else ["（暂无分组）"]
        recent = user_mgr.recent
        self._user_recent_combo["values"] = recent if recent else ["（暂无记录）"]

    def _on_user_group_selected(self, event=None):
        """选中用户分组时预览用户列表（不自动应用）"""
        name = self._user_group_var.get()
        if not name or name == "（暂无分组）":
            return
        g = user_mgr.get_group(name)
        if g:
            nicks = [u.get("nickname", u.get("user_id", "?")) for u in g.users[:5]]
            preview = ", ".join(nicks)
            if len(g.users) > 5:
                preview += f" …(共{len(g.users)}人)"
            self._append_log(f"👥 用户分组「{name}」: {preview}\n")

    def _apply_user_group(self):
        """将选中分组的用户列表替换当前抓取目标（支持合并）"""
        name = self._user_group_var.get()
        if not name or name == "（暂无分组）":
            messagebox.showinfo("提示", "请先选择一个用户分组")
            return
        g = user_mgr.get_group(name)
        if not g or not g.users:
            messagebox.showinfo("提示", f"分组「{name}」为空")
            return
        # 询问是替换还是合并
        existing = self.target_tree.get_children()
        if existing:
            choice = messagebox.askyesnocancel(
                "应用用户分组",
                f"将分组「{name}」({len(g.users)}人)应用到目标列表：\n\n"
                f"「是」= 替换当前列表\n"
                f"「否」= 合并到当前列表\n"
                f"「取消」= 不操作"
            )
            if choice is None:  # 取消
                return
            if not choice:  # 合并（否）
                # 获取现有 user_id 集合用于去重
                existing_ids = set()
                for item in existing:
                    v = self.target_tree.item(item, "values")
                    existing_ids.add(v[0])
                added = 0
                for u in g.users:
                    if u.get("user_id", "") not in existing_ids:
                        existing_ids.add(u.get("user_id", ""))
                        self.target_tree.insert("", END,
                            values=(u.get("user_id", ""), u.get("nickname", ""), ""))
                        added += 1
                self._save_targets(silent=True)
                self._append_log(f"👥 合并分组「{name}」: 新增 {added} 人 (现有 {len(existing_ids)} 人)\n")
                return
            # else: 替换（是），继续下面逻辑

        # 替换模式
        self.target_tree.delete(*self.target_tree.get_children())
        for u in g.users:
            self.target_tree.insert("", END,
                values=(u.get("user_id", ""), u.get("nickname", ""), ""))
        self._save_targets(silent=True)
        # 记录到最近使用
        display = f"{name}: " + ", ".join(
            u.get("nickname", u.get("user_id", "?")) for u in g.users[:10])
        if len(g.users) > 10:
            display += f" …共{len(g.users)}人"
        user_mgr.add_recent(display)
        self._refresh_user_group_ui()
        self._append_log(f"👥 已应用用户分组「{name}」({len(g.users)}人)\n")

    def _save_as_user_group(self):
        """将当前抓取目标列表保存为用户分组"""
        items = self.target_tree.get_children()
        if not items:
            messagebox.showwarning("提示", "目标列表为空，请先添加用户")
            return
        users = []
        for item in items:
            v = self.target_tree.item(item, "values")
            users.append({"user_id": v[0], "nickname": v[1]})

        dialog = Toplevel(self.root); dialog.title("保存用户分组")
        dialog.geometry("380x180"); dialog.resizable(False, False)
        dialog.transient(self.root); dialog.focus_set()
        Label(dialog, text="分组名称:").pack(pady=(12, 2))
        name_entry = Entry(dialog, width=40); name_entry.pack(padx=20, pady=2)
        name_entry.focus_set()
        Label(dialog, text="备注（可选）:", font=("", 8)).pack(pady=(8, 2))
        note_entry = Entry(dialog, width=40); note_entry.pack(padx=20, pady=2)

        def do_save():
            name = name_entry.get().strip()
            if not name:
                messagebox.showwarning("提示", "请输入分组名称", parent=dialog)
                return
            is_new = user_mgr.add_group(name, users, note_entry.get().strip())
            self._refresh_user_group_ui()
            self._user_group_var.set(name)
            act = "新建" if is_new else "更新（合并用户）"
            self._append_log(f"💾 {act}用户分组「{name}」({len(users)}人)\n")
            dialog.destroy()

        Button(dialog, text="保存", command=do_save).pack(pady=(12, 6))
        dialog.bind("<Return>", lambda e: do_save())

    def _manage_user_groups(self):
        """管理用户分组（查看/编辑/删除）"""
        dialog = Toplevel(self.root); dialog.title("管理用户分组")
        dialog.geometry("580x460"); dialog.resizable(True, True)
        dialog.transient(self.root); dialog.focus_set()
        list_frame = Frame(dialog); list_frame.pack(fill=BOTH, expand=True, padx=10, pady=10)
        Label(list_frame, text="已有分组（选中后可编辑/删除）:", font=("", 9)).pack(anchor="w")
        lb = Listbox(list_frame, height=8)
        lb.pack(fill=BOTH, expand=True, pady=4)
        for g in user_mgr.groups:
            lb.insert(END, f"{g.name}  ({len(g.users)}人)")
        Label(list_frame, text="用户列表（每行: user_id nickname）:", font=("", 9)).pack(anchor="w", pady=(8, 0))
        user_text = Text(list_frame, height=8); user_text.pack(fill=BOTH, expand=True, pady=4)
        btn_frame = Frame(list_frame); btn_frame.pack(fill=X, pady=6)

        def on_select(evt=None):
            sel = lb.curselection()
            if sel:
                g = user_mgr.groups[sel[0]]
                user_text.delete("1.0", END)
                lines = [f"{u.get('user_id', '')} {u.get('nickname', '')}" for u in g.users]
                user_text.insert("1.0", "\n".join(lines))
        lb.bind("<<ListboxSelect>>", on_select)

        def do_update():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个分组", parent=dialog); return
            g = user_mgr.groups[sel[0]]
            raw = user_text.get("1.0", END).strip()
            # 解析用户文本（每行 user_id nickname）
            new_users = []; seen = set()
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"): continue
                parts = line.split(None, 1)
                if parts:
                    uid = parts[0]
                    if uid in seen: continue
                    seen.add(uid)
                    nickname = parts[1].strip() if len(parts) > 1 else uid
                    new_users.append({"user_id": uid, "nickname": nickname})
            user_mgr.update_group_users(g.name, new_users)
            self._refresh_user_group_ui()
            self._append_log(f"✏ 已更新用户分组「{g.name}」({len(new_users)}人)\n")
            messagebox.showinfo("完成", f"分組「{g.name}」已更新", parent=dialog); dialog.destroy()

        def do_delete():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个分组", parent=dialog); return
            g = user_mgr.groups[sel[0]]
            if messagebox.askyesno("确认删除", f"确定要删除用户分组「{g.name}」吗？", parent=dialog):
                user_mgr.delete_group(g.name); self._refresh_user_group_ui()
                self._append_log(f"🗑 已删除用户分组「{g.name}」\n"); dialog.destroy()

        Button(btn_frame, text="💾 保存修改", command=do_update).pack(side=LEFT, padx=4)
        Button(btn_frame, text="🗑 删除分组", command=do_delete).pack(side=LEFT, padx=4)
        Button(btn_frame, text="关闭", command=dialog.destroy).pack(side=RIGHT, padx=4)
        if lb.size() > 0: lb.selection_set(0); on_select()

    def _import_users_file(self):
        """从文本文件导入用户列表"""
        filepath = filedialog.askopenfilename(
            title="选择用户列表文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if not filepath: return
        try:
            users = user_mgr.import_from_file(filepath)
            if not users:
                messagebox.showwarning("导入结果", "文件中未找到有效用户"); return
            # 询问替换或合并
            existing = self.target_tree.get_children()
            if existing:
                choice = messagebox.askyesnocancel(
                    "导入用户",
                    f"从 {Path(filepath).name} 导入了 {len(users)} 个用户：\n\n"
                    f"「是」= 替换当前列表\n"
                    f"「否」= 合并到当前列表\n"
                    f"「取消」= 不操作"
                )
                if choice is None: return
                if choice is False:  # 合并
                    existing_ids = set()
                    for item in existing:
                        v = self.target_tree.item(item, "values"); existing_ids.add(v[0])
                    added = 0
                    for u in users:
                        if u.get("user_id", "") not in existing_ids:
                            existing_ids.add(u.get("user_id", ""))
                            self.target_tree.insert("", END,
                                values=(u.get("user_id", ""), u.get("nickname", ""), ""))
                            added += 1
                    self._save_targets(silent=True)
                    self._append_log(f"📥 导入合并 {Path(filepath).name}: 新增 {added} 人\n")
                    return
                # else: 替换

            # 替换模式
            self.target_tree.delete(*self.target_tree.get_children())
            for u in users:
                self.target_tree.insert("", END,
                    values=(u.get("user_id", ""), u.get("nickname", ""), ""))
            self._save_targets(silent=True)
            self._append_log(f"📥 已导入 {len(users)} 个用户: {Path(filepath).name}\n")
        except Exception as e:
            messagebox.showerror("导入失败", f"读取文件出错:\n{e}")

    def _on_recent_users_selected(self, event=None):
        """选中最近使用的用户分组时应用"""
        val = self._user_recent_var.get()
        if not val or val == "（暂无记录）": return
        # 提取分组名（格式: "分组名: user1, user2, ..."）
        if ": " in val:
            group_name = val.split(": ", 1)[0]
            g = user_mgr.get_group(group_name)
            if g and g.users:
                # 替换当前列表
                self.target_tree.delete(*self.target_tree.get_children())
                for u in g.users:
                    self.target_tree.insert("", END,
                        values=(u.get("user_id", ""), u.get("nickname", ""), ""))
                self._save_targets(silent=True)
                self._user_group_var.set(group_name)
                self._append_log(f"🕐 已恢复用户分组「{group_name}」({len(g.users)}人)\n")

    # ── 关键词分组管理 ────────────────────────────────────

    def _refresh_keyword_ui(self):
        """刷新关键词分组下拉框和最近使用下拉框"""
        groups = keyword_mgr.group_names()
        self._group_combo["values"] = groups if groups else ["（暂无分组）"]
        recent = keyword_mgr.recent
        self._recent_combo["values"] = recent if recent else ["（暂无记录）"]

    def _on_keyword_enter(self, event=None):
        """关键词输入框回车：确认关键词并记录到最近使用"""
        kw = self.kw_var.get().strip()
        if kw:
            keyword_mgr.add_recent(kw)
            self._refresh_keyword_ui()
            self._append_log(f"🔍 关键词已确认: {kw}\n")

    def _on_group_selected(self, event=None):
        """选中分组时自动填入关键词到输入框"""
        name = self._group_var.get()
        if not name or name == "（暂无分组）":
            return
        g = keyword_mgr.get_group(name)
        if g:
            kw_str = ", ".join(g.keywords)
            self.kw_var.set(kw_str)
            self.kw_enabled_var.set(True)
            self._toggle_keyword()
            self._append_log(f"✅ 已应用分组「{name}」({len(g.keywords)}个关键词)\n")

    def _apply_group(self):
        """将选中分组的全部关键词填入关键词输入框"""
        name = self._group_var.get()
        if not name or name == "（暂无分组）":
            messagebox.showinfo("提示", "请先选择一个关键词分组")
            return
        g = keyword_mgr.get_group(name)
        if g:
            kw_str = ", ".join(g.keywords)
            self.kw_var.set(kw_str)
            self.kw_enabled_var.set(True)
            self._toggle_keyword()
            self._append_log(f"✅ 已应用分组「{name}」({len(g.keywords)}个关键词)\n")

    def _save_as_group(self):
        """将当前输入框的关键词保存为分组（若选中已有分组则预填名称，保存时替换而非合并）"""
        kw_str = self.kw_var.get().strip()
        if not kw_str:
            messagebox.showwarning("提示", "请先在关键词输入框中输入关键词")
            return
        dialog = Toplevel(self.root); dialog.title("保存关键词分组")
        dialog.geometry("380x180"); dialog.resizable(False, False)
        dialog.transient(self.root); dialog.focus_set()
        Label(dialog, text="分组名称:").pack(pady=(12, 2))
        name_entry = Entry(dialog, width=40); name_entry.pack(padx=20, pady=2)
        # 预填当前选中的分组名（如果有）
        current_name = self._group_var.get()
        if current_name and current_name != "（暂无分组）":
            name_entry.insert(0, current_name)
            name_entry.selection_range(0, END)
        name_entry.focus_set()
        Label(dialog, text="备注（可选）:", font=("", 8)).pack(pady=(8, 2))
        note_entry = Entry(dialog, width=40); note_entry.pack(padx=20, pady=2)

        def do_save():
            name = name_entry.get().strip()
            if not name:
                messagebox.showwarning("提示", "请输入分组名称", parent=dialog)
                return
            from weibo import _split_keywords
            kws = _split_keywords(kw_str)
            is_new = keyword_mgr.add_group(name, kws, note_entry.get().strip(), replace=True)
            self._refresh_keyword_ui(); self._group_var.set(name)
            act = "新建" if is_new else "更新（已替换）"
            self._append_log(f"💾 {act}分组「{name}」({len(kws)}个关键词)\n")
            dialog.destroy()

        Button(dialog, text="保存", command=do_save).pack(pady=(12, 6))
        dialog.bind("<Return>", lambda e: do_save())

    def _manage_groups(self):
        """管理关键词分组（查看/编辑/删除）"""
        dialog = Toplevel(self.root); dialog.title("管理关键词分组")
        dialog.geometry("520x420"); dialog.resizable(True, True)
        dialog.transient(self.root); dialog.focus_set()
        list_frame = Frame(dialog); list_frame.pack(fill=BOTH, expand=True, padx=10, pady=10)
        Label(list_frame, text="已有分组（选中目标→粘贴关键词→保存）:", font=("", 9)).pack(anchor="w")
        lb = Listbox(list_frame, height=8, exportselection=False)
        lb.pack(fill=BOTH, expand=True, pady=4)
        for g in keyword_mgr.groups:
            lb.insert(END, f"{g.name}  ({len(g.keywords)}个关键词)")
        Label(list_frame, text="关键词（任意分隔符均可）:", font=("", 9)).pack(anchor="w", pady=(8, 0))
        kw_text = Text(list_frame, height=5); kw_text.pack(fill=BOTH, expand=True, pady=4)
        btn_frame = Frame(list_frame); btn_frame.pack(fill=X, pady=6)

        def load_selected():
            """将选中分组的关键词加载到编辑区"""
            sel = lb.curselection()
            if sel:
                g = keyword_mgr.groups[sel[0]]
                kw_text.delete("1.0", END); kw_text.insert("1.0", ", ".join(g.keywords))
            else:
                messagebox.showwarning("提示", "请先在列表中选中一个分组", parent=dialog)

        # 显式"📥 加载"按钮才载入旧关键词，选择分组不会覆盖编辑区

        def do_update():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个分组", parent=dialog); return
            g = keyword_mgr.groups[sel[0]]
            from weibo import _split_keywords
            new_kws = _split_keywords(kw_text.get("1.0", END))
            keyword_mgr.update_group_keywords(g.name, new_kws)
            self._refresh_keyword_ui()
            # 同步更新主窗口：填入关键词 + 选中分组
            self.kw_var.set(", ".join(new_kws))
            self.kw_enabled_var.set(True)
            self._toggle_keyword()
            self._group_var.set(g.name)
            self._append_log(f"✏ 已更新分组「{g.name}」({len(new_kws)}个关键词)\n")
            messagebox.showinfo("完成", f"分组「{g.name}」已更新（已自动填入关键词输入框）", parent=dialog); dialog.destroy()

        def do_delete():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个分组", parent=dialog); return
            g = keyword_mgr.groups[sel[0]]
            if messagebox.askyesno("确认删除", f"确定要删除分组「{g.name}」吗？", parent=dialog):
                keyword_mgr.delete_group(g.name); self._refresh_keyword_ui()
                # 如果删除的是主窗口当前选中的分组，清除输入框
                if self._group_var.get() == g.name:
                    self._group_var.set("（暂无分组）" if not keyword_mgr.groups else keyword_mgr.groups[0].name)
                    self.kw_var.set("")
                    self.kw_enabled_var.set(False)
                    self._toggle_keyword()
                self._append_log(f"🗑 已删除分组「{g.name}」\n"); dialog.destroy()

        Button(btn_frame, text="📥 加载", command=load_selected).pack(side=LEFT, padx=4)
        Button(btn_frame, text="💾 保存修改", command=do_update).pack(side=LEFT, padx=4)
        Button(btn_frame, text="🗑 删除分组", command=do_delete).pack(side=LEFT, padx=4)
        Button(btn_frame, text="关闭", command=dialog.destroy).pack(side=RIGHT, padx=4)
        if lb.size() > 0: lb.selection_set(0)  # 初始高亮第一个，不填编辑区

    def _import_keywords_file(self):
        """从文本文件导入关键词"""
        filepath = filedialog.askopenfilename(
            title="选择关键词文件",
            filetypes=[("文本文件", "*.txt"), ("CSV文件", "*.csv"), ("所有文件", "*.*")]
        )
        if not filepath: return
        try:
            kws = keyword_mgr.import_from_file(filepath)
            if not kws:
                messagebox.showwarning("导入结果", "文件中未找到关键词"); return
            kw_str = ", ".join(kws)
            self.kw_var.set(kw_str); self.kw_enabled_var.set(True); self._toggle_keyword()
            self._append_log(f"📥 已导入 {len(kws)} 个关键词: {Path(filepath).name}\n")
        except Exception as e:
            messagebox.showerror("导入失败", f"读取文件出错:\n{e}")

    def _on_recent_selected(self, event=None):
        """选中最近使用的关键词时填入输入框"""
        kw = self._recent_var.get()
        if not kw or kw == "（暂无记录）": return
        self.kw_var.set(kw); self.kw_enabled_var.set(True); self._toggle_keyword()

    # ── 已抓取用户列表 ────────────────────────────────────

    def _refresh_crawled_users_report(self, silent=False):
        """刷新「已抓取用户列表.md」"""
        try:
            db_path = os.path.join(SCRIPT_DIR, "weibo", "weibodata.db")
            output_dir = self.config.get("output_directory", "output")
            output_root = os.path.join(SCRIPT_DIR, output_dir)
            uid_file = os.path.join(SCRIPT_DIR, "user_id_list.txt")

            # 读取用户列表
            users = {}
            if os.path.isfile(uid_file):
                with open(uid_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        p = line.strip().split(" ", 2)
                        if p and p[0].isdigit():
                            users[p[0]] = {"nickname": p[1] if len(p) > 1 else p[0]}

            # 从数据库读取抓取统计
            if os.path.isfile(db_path):
                con = sqlite3.connect(db_path)
                def _fetch(sql, *args):
                    cur = con.cursor(); cur.execute(sql, args); rows = cur.fetchall(); cur.close()
                    return rows
                for uid in users:
                    row = _fetch("SELECT MIN(created_at), MAX(created_at), COUNT(*) FROM weibo WHERE user_id=?", uid)
                    if row and row[0]:
                        users[uid]["first_crawl"] = row[0][0][:10] if row[0][0] else "-"
                        users[uid]["last_crawl"] = row[0][1][:10] if row[0][1] else "-"
                        users[uid]["weibo_count"] = row[0][2]
                # 扫描输出目录中的 MD 文件数
                for uid, info in users.items():
                    sn = info.get("nickname", uid)
                    safe_name = re.sub(r'[<>:"/\\|?*]', '_', sn)
                    user_dir = os.path.join(output_root, safe_name)
                    md_count = 0
                    if os.path.isdir(user_dir):
                        md_count = len([f for f in os.listdir(user_dir) if f.endswith('.md')])
                    info["md_files"] = md_count
                # 用户信息（从user表）
                for uid in users:
                    row = _fetch("SELECT follower_count, gender, location FROM user WHERE id=?", uid)
                    if row and row[0]:
                        users[uid]["followers"] = row[0][0] or 0
                        users[uid]["gender"] = row[0][1] or ""
                        users[uid]["location"] = row[0][2] or ""
                con.close()

            # 生成 MD 报告
            lines = ["# 📋 已抓取微博用户列表", ""]
            from datetime import datetime
            lines.append(f"> 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append("")

            crawled = {u: i for u, i in users.items() if i.get("weibo_count", 0) > 0}
            uncrawled = {u: i for u, i in users.items() if u not in crawled}
            total = sum(i.get("weibo_count", 0) for i in users.values())
            total_md = sum(i.get("md_files", 0) for i in users.values())

            lines.append(f"**总用户数**: {len(users)} | **已抓取**: {len(crawled)} | "
                        f"**累计微博**: {total} 条 | **现存 MD 文件**: {total_md} 个")
            lines.append("")

            if crawled:
                lines.append("## ✅ 已抓取用户")
                lines.append("")
                lines.append("| 序号 | 昵称 | 用户ID | 首次 | 最近 | 微博数 | MD数 |")
                lines.append("|------|------|--------|------|------|--------|------|")
                for i, (uid, info) in enumerate(sorted(crawled.items(), key=lambda x: x[1].get("weibo_count", 0), reverse=True), 1):
                    lines.append(f"| {i} | {info.get('nickname', uid)} | `{uid}` | "
                               f"{info.get('first_crawl', '-')} | {info.get('last_crawl', '-')} | "
                               f"{info.get('weibo_count', 0)} | {info.get('md_files', 0)} |")
                lines.append("")

            if uncrawled:
                lines.append("## ⏳ 待抓取用户")
                lines.append("")
                lines.append("| 序号 | 昵称 | 用户ID |")
                lines.append("|------|------|--------|")
                for i, (uid, info) in enumerate(uncrawled.items(), 1):
                    lines.append(f"| {i} | {info.get('nickname', uid)} | `{uid}` |")
                lines.append("")

            if crawled:
                lines.append("---")
                lines.append("")
                lines.append("## 📊 抓取详情")
                lines.append("")
                for uid, info in sorted(crawled.items(), key=lambda x: x[1].get("weibo_count", 0), reverse=True):
                    sn = info.get("nickname", uid)
                    lines.append(f"### {sn} (`{uid}`)")
                    lines.append("")
                    lines.append(f"- **首次抓取**: {info.get('first_crawl', '-')}")
                    lines.append(f"- **最近抓取**: {info.get('last_crawl', '-')}")
                    lines.append(f"- **抓取微博数**: {info.get('weibo_count', 0)} 条")
                    lines.append(f"- **现存 MD 文件**: {info.get('md_files', 0)} 个")
                    fl = info.get("followers", 0)
                    loc = info.get("location", "")
                    detail = []
                    if fl: detail.append(f"粉丝 {fl:,}")
                    if loc: detail.append(loc)
                    if detail: lines.append(f"- **用户信息**: {' / '.join(detail)}")
                    lines.append(f"- **主页**: https://weibo.com/u/{uid}")
                    lines.append("")

            report_path = os.path.join(SCRIPT_DIR, "已抓取用户列表.md")
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines))
            if not silent:
                self._append_log(f"📋 已刷新用户列表: 已抓取用户列表.md\n")
        except Exception as e:
            if not silent:
                self._append_log(f"⚠ 刷新用户列表失败: {e}\n")


def main():
    root = Tk()
    WeiboCrawlerGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
