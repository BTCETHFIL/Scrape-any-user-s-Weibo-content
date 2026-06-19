#!/usr/bin/env python
# -*- coding: utf-8 -*-

import copy, json, logging, logging.config, os, sys, threading, time, queue, re, sqlite3
from pathlib import Path
from tkinter import Tk, Toplevel, Frame, LabelFrame, Label, Button, Entry, Text, Scrollbar, messagebox, StringVar, IntVar, BooleanVar, filedialog, DISABLED, NORMAL, END, RIGHT, Y, BOTH, LEFT, X, TOP, BOTTOM, WORD, ttk, Spinbox, Checkbutton

SCRIPT_DIR = os.path.split(os.path.realpath(__file__))[0]
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import const
from weibo import Weibo, get_config as get_weibo_config, handle_config_renaming

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
        ud = os.path.join(SCRIPT_DIR, "weibo_data", sn)
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
        self._create_target_panel(); self._create_config_panel()
        self._create_log_panel(); self._create_status_bar()
        self.poll_interval = 500; self._poll_log_queue(); self._load_crawl_status()

    def _create_target_panel(self):
        frame = LabelFrame(self.root, text="抓取目标", padx=8, pady=4)
        frame.pack(fill=BOTH, padx=8, pady=4, expand=False)
        self.target_tree = ttk.Treeview(frame, columns=("uid","昵称","日期"), show="headings", height=5)
        for c in ("uid","昵称","日期"): self.target_tree.heading(c, text=c); self.target_tree.column(c, width=120)
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
        dlg.transient(self.root); dlg.grab_set()
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
        txt = ""
        for item in self.target_tree.get_children():
            v = self.target_tree.item(item, "values")
            txt += "{} {}\n".format(v[0], v[1])
        out = os.path.join(SCRIPT_DIR, "user_id_list.txt")
        with open(out, 'w', encoding='utf-8') as f: f.write(txt)
        self.config["user_id_list"] = out
        self.controller.save_config(self.config)
        if not silent: messagebox.showinfo("提示", "已保存到 {}".format(out))

    def _create_config_panel(self):
        frame = LabelFrame(self.root, text="抓取配置", padx=8, pady=4)
        frame.pack(fill=X, padx=8, pady=4)
        r1 = Frame(frame); r1.pack(fill=X, pady=2)
        Label(r1, text="模式:").pack(side=LEFT, padx=(0,2))
        self.mode_var = StringVar(value=const.MODE)
        ttk.Combobox(r1, textvariable=self.mode_var, values=["append","overwrite"], state="readonly", width=10).pack(side=LEFT, padx=2)
        Label(r1, text="每页:").pack(side=LEFT, padx=(12,2))
        self.ppc_var = IntVar(value=self.config.get("page_weibo_count", 10))
        Spinbox(r1, from_=1, to=50, textvariable=self.ppc_var, width=5).pack(side=LEFT, padx=2)
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
        Checkbutton(r2, text="仅原创", variable=self.orig_only_var).pack(side=LEFT, padx=4)
        self.pic_dl_var = BooleanVar(value=bool(self.config.get("original_pic_download", 1)))
        Checkbutton(r2, text="图片", variable=self.pic_dl_var).pack(side=LEFT, padx=4)
        self.video_dl_var = BooleanVar(value=bool(self.config.get("original_video_download", 1)))
        Checkbutton(r2, text="视频", variable=self.video_dl_var).pack(side=LEFT, padx=4)
        self.comment_dl_var = BooleanVar(value=bool(self.config.get("download_comment", 1)))
        Checkbutton(r2, text="评论", variable=self.comment_dl_var).pack(side=LEFT, padx=4)
        self.repost_dl_var = BooleanVar(value=bool(self.config.get("download_repost", 1)))
        Checkbutton(r2, text="转发", variable=self.repost_dl_var).pack(side=LEFT, padx=4)
        ab = self.config.get("anti_ban_config", {})
        self.anti_ban_var = BooleanVar(value=ab.get("enabled", True) if isinstance(ab, dict) else True)
        Checkbutton(r2, text="防封禁", variable=self.anti_ban_var).pack(side=LEFT, padx=4)
        r3 = Frame(frame); r3.pack(fill=X, pady=2)
        Label(r3, text="时间:").pack(side=LEFT, padx=(0,2))
        self.sd_var = StringVar(value=self.config.get("since_date") or "2024-12-20")
        Entry(r3, textvariable=self.sd_var, width=10, font=("", 9)).pack(side=LEFT, padx=2)
        Label(r3, text="至").pack(side=LEFT, padx=2)
        self.ed_var = StringVar(value=self.config.get("end_date") or "")
        Entry(r3, textvariable=self.ed_var, width=10, font=("", 9)).pack(side=LEFT, padx=2)
        self.all_range_var = BooleanVar(value=False)
        cb = Checkbutton(r3, text="全时段", variable=self.all_range_var, command=self._toggle_range)
        cb.pack(side=LEFT, padx=8)
        self._toggle_range()
        # ── 手动链接保存 ──
        manual_frame = Frame(self.root); manual_frame.pack(fill=X, padx=8, pady=(6, 2))
        Label(manual_frame, text="🔗 手动链接 (每行一条 · 上限20):", font=("", 9)).pack(anchor="w")
        self._manual_text = Text(manual_frame, height=4, font=("Consolas", 9),
                                  wrap=WORD, fg="gray")
        self._manual_text.insert("1.0", "粘贴微博链接，每行一条...\n如 https://weibo.com/xxx/xxx")
        self._manual_text.bind('<FocusIn>', self._on_manual_focus_in)
        self._manual_text.bind('<FocusOut>', self._on_manual_focus_out)
        self._manual_text.pack(fill=X, pady=(2, 0))
        btn_row2 = Frame(manual_frame); btn_row2.pack(fill=X, pady=(4, 0))
        self._manual_btn = Button(btn_row2, text="📝 批量保存为 MD", command=self._save_manual_weibos,
                                   bg="#607D8B", fg="white", width=16)
        self._manual_btn.pack(side=LEFT)
        ToolTip(self._manual_btn, "将上方粘贴的微博链接逐条抓取\n保存为 Markdown 文件\n支持 weibo.com / m.weibo.cn 链接\n最多同时处理 20 条")
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
        ToolTip(b, "扫描 weibo_data 目录中超过 10MB 的 Markdown 文件\n按二级标题（## ）自动分割为小文件\n分块命名：原文件名(1).md / (2).md ...")
        self.target_tree.bind("<<TreeviewSelect>>", lambda e: self._on_select_user())

    def _start(self):
        sel = self.target_tree.selection(); self._save_config(); self._save_targets()
        override = None
        if sel:
            v = self.target_tree.item(sel[0], "values")
            uid, name = v[0], v[1]
            if messagebox.askyesno("选择性抓取", "仅抓取 {}({})？".format(name, uid)):
                override = self.config.copy(); override["user_id_list"] = [uid]
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
        dlg.transient(self.root); dlg.grab_set()
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
        # 只保留 sqlite + markdown，不生成 csv/json
        self.config["write_mode"] = ["sqlite", "markdown"]
        const.MODE = self.mode_var.get()  # 同步模块级变量，确保 weibo.py 使用正确模式
        self.controller.save_config(self.config)

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
        d = filedialog.askdirectory(title="选择要扫描的 MD 文件目录",
            initialdir=os.path.join(SCRIPT_DIR, "weibo_data"))
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

    MAX_MANUAL_URLS = 20

    def _save_manual_weibos(self, event=None):
        """批量手动微博链接 → 保存为 MD"""
        raw = self._manual_text.get("1.0", END).strip()
        if not raw or raw.startswith("粘贴微博链接"):
            messagebox.showwarning("提示", "请先粘贴微博链接")
            return

        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        urls = [u for u in urls if 'weibo.com' in u or 'weibo.cn' in u]

        if not urls:
            messagebox.showwarning("提示", "请粘贴微博链接（weibo.com 或 m.weibo.cn）")
            return

        if len(urls) > self.MAX_MANUAL_URLS:
            urls = urls[:self.MAX_MANUAL_URLS]
            self._manual_text.delete("1.0", END)
            self._manual_text.insert("1.0", "\n".join(urls))

        if self.controller.state in ("running", "paused"):
            messagebox.showwarning("提示", "已有爬取任务在运行中，请等待完成")
            return

        self._manual_btn.config(state=DISABLED, text=f"保存中 (0/{len(urls)})...")
        self._save_config()

        def worker():
            success = 0
            fail = 0
            try:
                import weibo as wm
                config = self.controller.config
                wb = Weibo(config)
                total = len(urls)
                self.root.after(0, lambda: self._append_log(f"\n🔗 批量保存 {total} 条微博链接\n"))
                for i, url in enumerate(urls, 1):
                    self.root.after(0, lambda c=i, t=total: self._manual_btn.config(
                        state=DISABLED, text=f"保存中 ({c}/{t})..."))
                    self.root.after(0, lambda u=url: self._append_log(f"  [{i}/{total}] {u[:80]}...\n"))
                    result = wb.crawl_single_weibo_url(url)
                    if result['success']:
                        self.root.after(0, lambda p=result['md_path']: self._append_log(f"    ✅ {p}\n"))
                        success += 1
                    else:
                        self.root.after(0, lambda e=result['error']: self._append_log(f"    ❌ {e}\n"))
                        fail += 1
            except Exception as e:
                import traceback
                self.root.after(0, lambda: self._append_log(f"保存异常: {e}\n{traceback.format_exc()}\n"))
            finally:
                summary = f"✅ {success} 成功"
                if fail:
                    summary += f" / ❌ {fail} 失败"
                self.root.after(0, lambda s=summary: self._append_log(f"\n📊 批量保存完成: {s}\n"))
                self.root.after(0, lambda s=summary, t=len(urls): messagebox.showinfo(
                    "批量保存完成", f"批量保存 {t} 条完成:\n{s}"))
                self.root.after(0, lambda: self._manual_btn.config(state=NORMAL, text="📝 批量保存为 MD"))

        threading.Thread(target=worker, daemon=True).start()

    def _create_log_panel(self):
        frame = LabelFrame(self.root, text="运行日志", padx=8, pady=4)
        frame.pack(fill=BOTH, expand=True, padx=8, pady=4)
        self.log_text = Text(frame, wrap="word", height=10, state=DISABLED, font=("Consolas", 9))
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


def main():
    root = Tk()
    WeiboCrawlerGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
