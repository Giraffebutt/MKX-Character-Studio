#!/usr/bin/env python3
"""
MKX Character Studio - a friendly front-end for mkx_meshmod.py

  * Export an original Mortal Kombat X character to .glb (+ textures) to use as a Blender reference.
  * Convert your own rigged .glb (+ textures) into Mortal Kombat X's character package format.

Everything is written inside your chosen mod folder; the game's own folders are only ever read.
Double-click "Launch Studio.cmd".
"""
import os, sys, json, re, shutil, threading, queue, traceback, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mkx_meshmod as mk  # noqa: E402

from studio_core import *
SETTINGS_FILE = os.path.join(HERE, 'studio_settings.json')
NO_MODEL = '(none)'
ABOUT_TITLE = 'Unofficial Modding Tool'
ABOUT_TEXT = ('MKX Character Studio is an independent community project and is not affiliated with or endorsed by '
              'Warner Bros. Games or NetherRealm Studios. No Mortal Kombat X game assets are distributed with this '
              'software. Users must provide their own legally obtained copy of the game. Mortal Kombat and related '
              'intellectual property belong to their respective owners. Use of this software is at the user\'s own '
              'risk and subject to applicable laws and license agreements.')

# ============================================================================================ GUI
def run_gui(selftest=False):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from tkinter.scrolledtext import ScrolledText

    class Studio(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(APP + ' (unofficial community tool)')
            self.geometry('1120x820'); self.minsize(980, 720)
            self.protocol('WM_DELETE_WINDOW', self.close)
            self.q = queue.Queue(); self.busy = False; self.buttons = []
            self.settings = self.load_settings()
            self.ws = tk.StringVar(value=self.settings.get('workspace', ''))
            self.game = tk.StringVar(value=self.settings.get('game_dir') or detect_game_dir())
            self.build()
            self.withdraw()
            self.after(50, self.deiconify)
            self.after(100, self.poll)
            if not selftest:
                self.after(300, self.first_run)

        # ---------------------------------------------------------------- settings
        def load_settings(self):
            try:
                return json.load(open(SETTINGS_FILE, encoding='utf-8'))
            except Exception:
                return {}

        def save_settings(self):
            self.settings.update(workspace=self.ws.get(), game_dir=self.game.get())
            try:
                json.dump(self.settings, open(SETTINGS_FILE, 'w', encoding='utf-8'), indent=2)
            except Exception:
                pass

        # ---------------------------------------------------------------- layout
        def build(self):
            pad = dict(padx=6, pady=4)
            menubar = tk.Menu(self)
            helpmenu = tk.Menu(menubar, tearoff=0)
            helpmenu.add_command(label='About %s' % APP, command=self.show_about)
            menubar.add_cascade(label='Help', menu=helpmenu)
            self.config(menu=menubar)
            self.helpmenu = helpmenu
            top = ttk.LabelFrame(self, text='Folders'); top.pack(fill='x', **pad)
            ttk.Label(top, text='Mod folder:').grid(row=0, column=0, sticky='w', **pad)
            ttk.Entry(top, textvariable=self.ws, state='readonly').grid(row=0, column=1, sticky='ew', **pad)
            self.btn(top, 'Choose...', self.choose_ws).grid(row=0, column=2, **pad)
            self.btn(top, 'Open', lambda: self.open_dir(self.ws.get())).grid(row=0, column=3, **pad)
            ttk.Label(top, text='Game folder (read only):').grid(row=1, column=0, sticky='w', **pad)
            ttk.Entry(top, textvariable=self.game, state='readonly').grid(row=1, column=1, sticky='ew', **pad)
            self.btn(top, 'Choose...', self.choose_game).grid(row=1, column=2, **pad)
            self.btn(top, 'Add original package...', self.add_original).grid(row=1, column=3, **pad)
            top.columnconfigure(1, weight=1)
            ttk.Label(self, text='Replaces a character\'s main mesh and textures, keeping its original skeleton. '
                      'Your model must already be rigged to that skeleton.',
                      wraplength=1040).pack(fill='x', padx=12, pady=4)

            nb = ttk.Notebook(self); nb.pack(fill='both', expand=True, **pad)
            self.build_export(nb); self.build_convert(nb); self.build_pmsk(nb); self.build_extract(nb)

            bottom = ttk.Frame(self); bottom.pack(fill='both', **pad)
            self.prog = ttk.Progressbar(bottom, mode='indeterminate'); self.prog.pack(fill='x')
            self.logbox = ScrolledText(bottom, height=7, state='disabled', font=('Consolas', 9)); self.logbox.pack(fill='both', expand=True)

        def btn(self, parent, text, cmd):
            b = ttk.Button(parent, text=text, command=cmd); self.buttons.append(b); return b

        def build_export(self, nb):
            f = ttk.Frame(nb); nb.add(f, text='  1. Export original character  ')
            pad = dict(padx=8, pady=6)
            ttk.Label(f, text='Exports an original character as .glb (skeleton + textured mesh) and its textures into\n'
                              'vanilla_exports\\ - import that .glb into Blender as the base for your own character.').grid(row=0, column=0, columnspan=4, sticky='w', **pad)
            ttk.Label(f, text='Character package:').grid(row=1, column=0, sticky='w', **pad)
            self.e_pkg = tk.StringVar(); self.e_all = tk.BooleanVar(value=False)
            self.e_pkg_cb = ttk.Combobox(f, textvariable=self.e_pkg, state='readonly', width=48)
            self.e_pkg_cb.grid(row=1, column=1, sticky='w', **pad)
            self.e_pkg_cb.bind('<<ComboboxSelected>>', lambda e: self.load_meshes(self.e_pkg.get(), self.e_mesh_cb, self.e_mesh,
                                                                                 then=self.load_export_objects))
            ttk.Checkbutton(f, text='show all packages', variable=self.e_all, command=self.refresh_packages).grid(row=1, column=2, sticky='w', **pad)
            ttk.Label(f, text='Mesh:').grid(row=2, column=0, sticky='w', **pad)
            self.e_mesh = tk.StringVar()
            self.e_mesh_cb = ttk.Combobox(f, textvariable=self.e_mesh, state='readonly', width=48)
            self.e_mesh_cb.grid(row=2, column=1, sticky='w', **pad)
            self.e_mesh_cb.bind('<<ComboboxSelected>>', lambda e: self.load_export_objects())
            self.e_tex = tk.BooleanVar(value=True); self.e_embed = tk.BooleanVar(value=True)
            ttk.Checkbutton(f, text='Export textures (PNG + DDS)', variable=self.e_tex).grid(row=3, column=1, sticky='w', **pad)
            ttk.Checkbutton(f, text='Show textures on the model in Blender (embed diffuse in the .glb)', variable=self.e_embed).grid(row=4, column=1, sticky='w', **pad)
            self.e_blend = tk.BooleanVar(value=self.settings.get('make_blend', True))
            ttk.Checkbutton(f, text='Also create a .blend with bones pointing at their children (uses Blender)', variable=self.e_blend).grid(row=5, column=1, sticky='w', **pad)
            self.btn(f, 'Blender...', self.choose_blender).grid(row=5, column=2, sticky='w', **pad)
            objects = ttk.LabelFrame(f, text='Also export these extra objects (hats, weapons...): tick the ones you want')
            objects.grid(row=6, column=0, columnspan=4, sticky='ew', **pad)
            self.e_objframe = ttk.Frame(objects); self.e_objframe.pack(fill='x', padx=6, pady=2)
            self.e_obj_vars = self.fill_checks(self.e_objframe, note='Pick a character package first.')
            self.btn(f, 'Export to .glb', self.do_export).grid(row=7, column=1, sticky='w', **pad)
            self.btn(f, 'Open vanilla_exports', lambda: self.open_dir(os.path.join(self.ws.get(), 'vanilla_exports'))).grid(row=7, column=2, sticky='w', **pad)
            f.columnconfigure(3, weight=1)

        def build_convert(self, nb):
            f = ttk.Frame(nb); nb.add(f, text='  2. Convert custom character  ')
            pad = dict(padx=8, pady=5)
            ttk.Label(f, text='Base character:').grid(row=0, column=0, sticky='w', **pad)
            self.c_pkg = tk.StringVar()
            self.c_pkg_cb = ttk.Combobox(f, textvariable=self.c_pkg, state='readonly', width=40)
            self.c_pkg_cb.grid(row=0, column=1, sticky='w', **pad)
            self.c_pkg_cb.bind('<<ComboboxSelected>>', lambda e: self.load_targets())
            ttk.Label(f, text='Mesh to replace:').grid(row=0, column=2, sticky='e', **pad)
            self.c_mesh = tk.StringVar()
            self.c_mesh_cb = ttk.Combobox(f, textvariable=self.c_mesh, state='readonly', width=58)
            self.c_mesh_cb.grid(row=0, column=3, sticky='w', **pad)
            self.c_mesh_cb.bind('<<ComboboxSelected>>', lambda e: self.switch_mesh())
            ttk.Label(f, text='Your model for this mesh (.glb):').grid(row=1, column=0, sticky='w', **pad)
            self.c_model = tk.StringVar(value=NO_MODEL)
            self.c_model_cb = ttk.Combobox(f, textvariable=self.c_model, state='readonly', width=40, values=[NO_MODEL])
            self.c_model_cb.grid(row=1, column=1, sticky='w', **pad)
            self.c_model_cb.bind('<<ComboboxSelected>>', lambda e: self.model_chosen())
            self.btn(f, 'Refresh files', self.refresh_files).grid(row=1, column=2, sticky='e', **pad)
            self.btn(f, 'Import GLB...', self.import_glb).grid(row=1, column=3, sticky='w', **pad)
            self.c_summary = ttk.Label(f, text='', wraplength=1040)
            self.c_summary.grid(row=2, column=0, columnspan=4, sticky='w', padx=8)
            self.c_paths, self.c_default, self.cur_mesh = {}, '', None

            texture_panel = ttk.LabelFrame(f, text='Textures (choose what replaces each original texture)')
            texture_panel.grid(row=3, column=0, columnspan=4, sticky='nsew', **pad)
            canvas = tk.Canvas(texture_panel, height=150, highlightthickness=0)
            scroll = ttk.Scrollbar(texture_panel, orient='vertical', command=canvas.yview)
            canvas.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right', fill='y'); canvas.pack(side='left', fill='both', expand=True)
            self.texframe = ttk.Frame(canvas)
            interior = canvas.create_window((0, 0), window=self.texframe, anchor='nw')
            self.texframe.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
            canvas.bind('<Configure>', lambda event: canvas.itemconfigure(interior, width=event.width))
            self.texframe.columnconfigure(0, weight=1)
            self.tex_rows = {}
            ttk.Label(self.texframe, text='Pick a base character first.').grid(row=0, column=0, sticky='w', padx=6, pady=4)

            objects = ttk.LabelFrame(f, text='Extra objects (hats, weapons...): untick to hide one in the game')
            objects.grid(row=4, column=0, columnspan=4, sticky='ew', **pad)
            self.objframe = ttk.Frame(objects); self.objframe.pack(fill='x', padx=6, pady=2)
            self.obj_vars = self.fill_checks(self.objframe, note='Pick a base character first.')

            opt = ttk.LabelFrame(f, text='Options'); opt.grid(row=5, column=0, columnspan=4, sticky='ew', **pad)
            self.o_darken = tk.BooleanVar(value=True); self.o_uv2 = tk.StringVar(value=UV2_CHOICES['auto'])
            self.o_force = tk.BooleanVar(value=False); self.o_preview = tk.BooleanVar(value=True); self.o_keep = tk.BooleanVar(value=False); self.o_wrinkles = tk.BooleanVar(value=True)
            ttk.Checkbutton(opt, text="Darken diffuse textures that are much brighter than MKX's (they glow under the game's lighting)", variable=self.o_darken).grid(row=0, column=0, sticky='w', padx=6)
            uv2 = ttk.Frame(opt); uv2.grid(row=1, column=0, sticky='w', padx=6)
            ttk.Label(uv2, text='Blood and damage (2nd UV map):').pack(side='left')
            ttk.Combobox(uv2, textvariable=self.o_uv2, state='readonly', width=62, values=list(UV2_CHOICES.values())).pack(side='left', padx=6)
            ttk.Checkbutton(opt, text='Ignore the skeleton position check (only if you know the armature was not changed)', variable=self.o_force).grid(row=2, column=0, sticky='w', padx=6)
            ttk.Checkbutton(opt, text="Turn off the original character's face wrinkles (they are painted for the original face)", variable=self.o_wrinkles).grid(row=5, column=0, sticky='w', padx=6)
            ttk.Checkbutton(opt, text="Experimental: keep my images' own size instead of resizing them to the original textures' (not tested in the game)", variable=self.o_keep).grid(row=4, column=0, sticky='w', padx=6)
            ttk.Checkbutton(opt, text='Also create a textured preview .glb of the result', variable=self.o_preview).grid(row=3, column=0, sticky='w', padx=6)
            row = ttk.Frame(f); row.grid(row=6, column=0, columnspan=4, sticky='w', **pad)
            ttk.Label(row, text='Output name:').pack(side='left')
            self.c_out = tk.StringVar(); ttk.Entry(row, textvariable=self.c_out, width=30).pack(side='left', padx=6)
            self.btn(row, 'Convert to Mortal Kombat X format', self.do_convert).pack(side='left', padx=6)
            self.btn(row, 'Import textures...', self.import_textures).pack(side='left', padx=6)
            self.btn(row, 'Open converted folder', lambda: self.open_dir(os.path.join(self.ws.get(), 'converted'))).pack(side='left', padx=6)
            f.columnconfigure(1, weight=1); f.rowconfigure(3, weight=1)

        def build_pmsk(self, nb):
            f = ttk.Frame(nb); nb.add(f, text='  3. Make Pmsk mask  ')
            pad = dict(padx=8, pady=4)
            ttk.Label(f, text='Body editing mode uses the working channel interpretation; exact shader behavior still needs validation. '
                              'Empty rows use defaults. Raw mode preserves four equally sized grayscale channels without rescaling '
                              'or resizing; all four are required. Use raw exports for hair, eyes or exact decoded-pixel reconstruction.',
                      wraplength=1040).grid(row=0, column=0, columnspan=5, sticky='w', **pad)
            mode_row = ttk.Frame(f); mode_row.grid(row=1, column=0, columnspan=5, sticky='w', **pad)
            ttk.Label(mode_row, text='Input mode:').pack(side='left')
            self.m_mode = tk.StringVar(value='Body editing masks')
            mode = ttk.Combobox(mode_row, textvariable=self.m_mode, state='readonly', width=26,
                                values=('Body editing masks', 'Raw channels'))
            mode.pack(side='left', padx=6)
            mode.bind('<<ComboboxSelected>>', lambda e: self.update_pmsk_mode())
            self.m_layers = {}
            self.m_labels, self.m_help = {}, {}
            for i, (key, label, help_text) in enumerate(PMSK_LAYERS):
                var = tk.StringVar(); self.m_layers[key] = var
                self.m_labels[key] = ttk.Label(f, text=label, width=24)
                self.m_labels[key].grid(row=i + 2, column=0, sticky='w', **pad)
                ttk.Entry(f, textvariable=var, state='readonly', width=24).grid(row=i + 2, column=1, sticky='ew', **pad)
                self.btn(f, 'Browse...', lambda v=var, k=key: self.pick_layer(v, self.m_labels[k].cget('text'))).grid(row=i + 2, column=2, pady=4)
                self.btn(f, 'Clear', lambda v=var: v.set('')).grid(row=i + 2, column=3, pady=4)
                self.m_help[key] = ttk.Label(f, text=help_text, wraplength=420)
                self.m_help[key].grid(row=i + 2, column=4, sticky='w', **pad)
            row = ttk.Frame(f); row.grid(row=len(PMSK_LAYERS) + 2, column=0, columnspan=5, sticky='w', **pad)
            ttk.Label(row, text="Name (your .glb's name, so tab 2 picks it):").pack(side='left')
            self.m_name = tk.StringVar(); ttk.Entry(row, textvariable=self.m_name, width=24).pack(side='left', padx=6)
            self.btn(row, 'Make Pmsk', self.do_make_pmsk).pack(side='left', padx=6)
            self.btn(row, 'Open textures folder', lambda: self.open_dir(os.path.join(self.ws.get(), 'textures'))).pack(side='left', padx=6)
            f.columnconfigure(1, weight=1)

        def update_pmsk_mode(self):
            rows = PMSK_RAW_LAYERS if self.m_mode.get() == 'Raw channels' else PMSK_LAYERS
            for key, label, help_text in rows:
                self.m_labels[key].configure(text=label)
                self.m_help[key].configure(text=help_text)

        def build_extract(self, nb):
            f = ttk.Frame(nb); nb.add(f, text='  4. Extract Pmsk  ')
            pad = dict(padx=8, pady=6)
            ttk.Label(f, text='Saves the decoded RGBA texture and four raw grayscale channels for exact reconstruction in tab 3 '
                              'Raw channels mode. Body materials also get normalized editing masks (red is rescaled/clipped), '
                              'approximate previews and a material report. Channel meanings are working interpretations. '
                              'Saved in vanilla_exports\\.', wraplength=900).grid(row=0, column=0, columnspan=4, sticky='w', **pad)
            ttk.Label(f, text='Character package:').grid(row=1, column=0, sticky='w', **pad)
            self.x_pkg = tk.StringVar(); self.x_all = tk.BooleanVar(value=False)
            self.x_pkg_cb = ttk.Combobox(f, textvariable=self.x_pkg, state='readonly', width=48)
            self.x_pkg_cb.grid(row=1, column=1, sticky='w', **pad)
            self.x_pkg_cb.bind('<<ComboboxSelected>>', lambda e: self.load_meshes(self.x_pkg.get(), self.x_mesh_cb, self.x_mesh))
            ttk.Label(f, text='Mesh:').grid(row=2, column=0, sticky='w', **pad)
            self.x_mesh = tk.StringVar()
            self.x_mesh_cb = ttk.Combobox(f, textvariable=self.x_mesh, state='readonly', width=48)
            self.x_mesh_cb.grid(row=2, column=1, sticky='w', **pad)
            self.btn(f, 'Extract Pmsk', self.do_extract).grid(row=3, column=1, sticky='w', **pad)
            self.btn(f, 'Open vanilla_exports', lambda: self.open_dir(os.path.join(self.ws.get(), 'vanilla_exports'))).grid(row=3, column=2, sticky='w', **pad)

        def show_about(self):
            messagebox.showinfo('About ' + APP, ABOUT_TITLE + '\n\n' + ABOUT_TEXT, parent=self)

        def close(self):
            if self.busy:
                messagebox.showinfo(APP, 'Please wait for the current operation to finish before closing.')
                return
            self.destroy()

        def import_glb(self):
            if not self.need_ready(): return
            source = filedialog.askopenfilename(title='Import rigged character GLB', filetypes=[('Binary glTF', '*.glb')])
            if not source: return
            ws, game = self.ws.get(), self.game.get()
            def done(name):
                self.refresh_files()
                if self.cur_mesh:
                    self.c_model.set(name); self.model_chosen()
            self.run(lambda: import_model(ws, source, game, self.log), done)

        def import_textures(self):
            if not self.need_ready(): return
            sources = filedialog.askopenfilenames(title='Import custom textures', filetypes=[('Images', ' '.join('*' + e for e in IMAGE_EXT))])
            if not sources: return
            ws, game, model = self.ws.get(), self.game.get(), self.c_model.get().replace(NO_MODEL, '')
            self.run(lambda: import_images(ws, sources, model, game), lambda _: self.refresh_files())

        # ---------------------------------------------------------------- helpers
        def log(self, msg):
            self.q.put(('log', str(msg)))

        def _append(self, msg):
            self.logbox.configure(state='normal'); self.logbox.insert('end', msg + '\n'); self.logbox.see('end')
            self.logbox.configure(state='disabled')

        def poll(self):
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    if kind == 'log':
                        self._append(payload)
                    elif kind == 'done':
                        cb, result = payload; self.set_busy(False)
                        if cb: cb(result)
                    elif kind == 'error':
                        exc, tb = payload; self.set_busy(False)
                        if not isinstance(exc, mk.MKXError):
                            self._append(tb)
                        self._append('ERROR: %s' % exc)
                        messagebox.showerror(APP, str(exc))
            except queue.Empty:
                pass
            self.after(100, self.poll)

        def set_busy(self, busy):
            self.busy = busy
            if busy:
                self.widget_states = []
                def lock(widget):
                    for w in widget.winfo_children():
                        if isinstance(w, (ttk.Button, ttk.Combobox, ttk.Checkbutton, ttk.Entry)):
                            self.widget_states.append((w, str(w.cget('state'))))
                            w.configure(state='disabled')
                        lock(w)
                lock(self)
            else:
                for w, state in getattr(self, 'widget_states', []):
                    if w.winfo_exists(): w.configure(state=state)
            if busy:
                self.prog.start(12)
            else:
                self.prog.stop()

        def run(self, fn, done=None):
            if self.busy:
                return
            self.set_busy(True)

            def work():
                try:
                    r = fn()
                    self.q.put(('done', (done, r)))
                except BaseException as ex:  # noqa: BLE001 - report everything to the user
                    self.q.put(('error', (ex, traceback.format_exc())))
            threading.Thread(target=work, daemon=True).start()

        def open_dir(self, path):
            if path and os.path.isdir(path):
                os.startfile(path)

        def need_ready(self):
            if not self.ws.get() or not os.path.isdir(self.ws.get()):
                messagebox.showwarning(APP, 'Choose your mod folder first.'); return False
            try:
                setup_workspace(self.ws.get(), self.game.get())
            except Exception as exc:
                messagebox.showerror(APP, str(exc)); return False
            return True

        # ---------------------------------------------------------------- folder actions
        def first_run(self):
            missing = []
            try:
                import numpy, PIL  # noqa: F401
            except ImportError:
                missing.append('numpy pillow')
            if missing:
                self._append('NOTE: texture conversion needs extra packages. Install them with:  python -m pip install numpy pillow')
            if not self.game.get():
                messagebox.showinfo(APP, 'Please choose your Mortal Kombat X game folder (it is only read, never changed).')
                self.choose_game()
            if not self.ws.get() or not os.path.isdir(self.ws.get()):
                messagebox.showinfo(APP, 'Welcome! Please choose (or create) a mod folder.\n\nThe tool will create these folders in it:\n'
                                    + '\n'.join('  %s\\ - %s' % (n, d) for n, d in FOLDERS))
                self.choose_ws()
            else:
                self.activate_ws(self.ws.get())

        def choose_ws(self):
            d = filedialog.askdirectory(title='Choose your mod folder', initialdir=self.ws.get() or str(HERE / 'projects'))
            if d:
                self.activate_ws(os.path.normpath(d))

        def activate_ws(self, d):
            if inside_game_content(d, self.game.get()):
                messagebox.showerror(APP, 'Choose a folder outside the game installation, or inside:\n%s' % (HERE / 'projects')); return
            try:
                setup_workspace(d, self.game.get())
            except Exception as exc:
                messagebox.showerror(APP, str(exc)); return
            self.ws.set(d); self.save_settings()
            self._append('mod folder ready: %s  (%s)' % (d, ', '.join(n for n, _ in FOLDERS)))
            self.clear_selection(); self.refresh_packages(); self.refresh_files()

        def choose_game(self):
            d = filedialog.askdirectory(title='Choose the Mortal Kombat X folder (contains Asset and Binaries)', initialdir=self.game.get() or 'C:\\')
            if d:
                if not os.path.isdir(os.path.join(d, 'Asset')):
                    messagebox.showerror(APP, 'That folder has no Asset subfolder - choose the MK10 game folder.'); return
                self.game.set(os.path.normpath(d)); self.clear_selection(); self.save_settings(); self.refresh_packages()

        def choose_blender(self):
            current = find_blender(self.game.get(), self.settings.get('blender_exe', ''))
            exe = filedialog.askopenfilename(title='Choose blender.exe', initialdir=os.path.dirname(current) or None,
                                             filetypes=[('Blender', 'blender.exe'), ('Programs', '*.exe')])
            if exe:
                self.settings['blender_exe'] = os.path.normpath(exe); self.save_settings()
                self.log('Blender: %s' % self.settings['blender_exe'])

        def add_original(self):
            if not self.ws.get():
                messagebox.showwarning(APP, 'Choose your mod folder first.'); return
            path = filedialog.askopenfilename(title='Choose an ORIGINAL character package (.xxx)', filetypes=[('MKX package', '*.xxx')])
            if path:
                ws, game = self.ws.get(), self.game.get()
                self.run(lambda: add_original_package(ws, path, self.log, game), lambda r: self.refresh_packages())

        def clear_selection(self):
            for var in (self.e_pkg, self.e_mesh, self.c_pkg, self.c_mesh, self.x_pkg, self.x_mesh): var.set('')
            self.e_mesh_cb['values'] = []; self.c_mesh_cb['values'] = []; self.x_mesh_cb['values'] = []
            for w in self.texframe.winfo_children(): w.destroy()
            self.tex_rows = {}; self.cur_mesh = None; self.c_paths = {}
            self.show_objects(note='Pick a base character first.')
            self.e_obj_vars = self.fill_checks(self.e_objframe, note='Pick a character package first.')
            self.update_summary()

        def refresh_packages(self):
            names = list_character_packages(self.game.get(), self.e_all.get()) if self.game.get() else []
            cached = os.path.join(self.ws.get(), 'vanilla_cache') if self.ws.get() else ''
            if cached and os.path.isdir(cached):
                names = sorted(set(names) | {f for f in os.listdir(cached) if f.lower().endswith('.xxx')}, key=str.lower)
            self.e_pkg_cb['values'] = names
            self.c_pkg_cb['values'] = [n for n in names if n.lower().startswith('char_')] or names
            self.x_pkg_cb['values'] = names

        def refresh_files(self):
            if not self.ws.get():
                return
            self.c_model_cb['values'] = [NO_MODEL] + list_models(self.ws.get())
            if self.c_model.get() not in self.c_model_cb['values']:
                self.c_model.set(NO_MODEL)
            self.images = list_images(self.ws.get())
            for tex, (cb, var, slot) in self.tex_rows.items():
                cb['values'] = self.row_values(slot)
            self.auto_textures(only_unchosen=True)     # new images fill textures still on "keep original"
            self.save_current()

        # ---------------------------------------------------------------- package / mesh / slots
        def load_meshes(self, pkg, combo, var, then=None):
            if not pkg or not self.need_ready():
                return

            def done(r):
                combo['values'] = r['meshes']; var.set(r['default'])
                if then: then()
            var.set(''); combo['values'] = []
            ws, game = self.ws.get(), self.game.get()
            self.run(lambda: job_load_package(ws, game, pkg, self.log), done)

        def row_values(self, slot):
            extra = [FLAT] if slot['param'] == 'NormalMap' else [NEUTRAL] if slot['param'] == 'Pmsk' else []
            return [KEEP] + extra + list(getattr(self, 'images', []))

        # ---------------------------------------------------------------- Convert tab: one saved setup per mesh
        # Each mesh to replace (the character and every extra object) keeps its own .glb and texture choices, so
        # switching meshes never carries one mesh's model over to another. Saved per mod folder and character.
        def choices(self):
            key = '%s|%s' % (os.path.normcase(self.ws.get()), self.c_pkg.get())
            st = self.settings.setdefault('convert_choices', {}).setdefault(key, {})
            st.setdefault('meshes', {}); st.setdefault('hidden', [])
            return st

        def save_current(self):
            if not self.c_pkg.get() or not self.c_paths:
                return
            st = self.choices()
            if self.cur_mesh:
                model = self.c_model.get()
                st['meshes'][self.cur_mesh] = dict(model='' if model in ('', NO_MODEL) else model,
                                                   textures={t: v.get() for t, (cb, v, s) in self.tex_rows.items() if v.get() != KEEP})
                st['current'] = self.cur_mesh
            if self.obj_vars:
                st['hidden'] = [p for p, v in self.obj_vars.items() if not v.get()]
            self.update_summary(); self.save_settings()

        def update_summary(self):
            if not self.c_paths:
                self.c_summary.configure(text=''); return
            names = {p: lbl for lbl, p in self.c_paths.items()}
            st = self.choices()
            linked = ['%s  <-  %s' % (names[p], m['model']) for p, m in st['meshes'].items() if m.get('model') and p in names]
            hidden = [names.get(p, p.split('.')[-1]) for p in st['hidden']]
            text = ('Will be converted:  ' + ';   '.join(linked)) if linked else 'No .glb linked yet: pick a mesh to replace, then the .glb that replaces it.'
            if hidden:
                text += '\nWill be hidden:  ' + ';   '.join(hidden)
            self.c_summary.configure(text=text)

        def load_targets(self):
            pkg = self.c_pkg.get()
            if not pkg or not self.need_ready():
                return
            self.cur_mesh = None; self.c_paths = {}; self.c_mesh.set(''); self.c_mesh_cb['values'] = []
            for w in self.texframe.winfo_children(): w.destroy()
            self.tex_rows = {}
            self.show_objects(note='Looking for extra objects...'); self.update_summary()

            def done(r):
                self.c_paths = {t['label']: t['path'] for t in r['targets']}
                self.c_default = r['default']
                self.c_mesh_cb['values'] = list(self.c_paths)
                self.show_objects(r['objects'])
                current = self.choices().get('current')
                label = next((l for l, p in self.c_paths.items() if p == current), next(iter(self.c_paths)))
                self.c_mesh.set(label); self.switch_mesh(save=False)
            ws, game = self.ws.get(), self.game.get()
            self.run(lambda: job_convert_targets(ws, game, pkg, self.log), done)

        def switch_mesh(self, save=True):
            if save:
                self.save_current()
            pkg, mesh = self.c_pkg.get(), self.c_paths.get(self.c_mesh.get())
            if not pkg or not mesh:
                return
            self.cur_mesh = None

            def done(r):
                for w in self.texframe.winfo_children():
                    w.destroy()
                self.tex_rows = {}
                ttk.Label(self.texframe, text='Material slots: ' + ', '.join('%d=%s' % (i, m) for i, m in enumerate(r['materials'])),
                          wraplength=900).grid(row=0, column=0, columnspan=2, sticky='w', padx=6, pady=3)
                if not r['slots']:
                    ttk.Label(self.texframe, text='This mesh has no textures that can be replaced here.').grid(row=1, column=0, sticky='w', padx=6)
                for i, s in enumerate(r['slots']):
                    label = '%s  -  %s  (%dx%d, slots %s)' % (ROLE_NAMES.get(s['param'], s['param']), s['texture'].split('.')[-1],
                                                             s['size'][0], s['size'][1], ','.join(map(str, s['slots'])))
                    ttk.Label(self.texframe, text=label, wraplength=510).grid(row=i + 1, column=0, sticky='w', padx=6, pady=2)
                    var = tk.StringVar(value=KEEP)
                    cb = ttk.Combobox(self.texframe, textvariable=var, state='readonly' if s['replaceable'] else 'disabled', width=46,
                                      values=self.row_values(s))
                    cb.grid(row=i + 1, column=1, sticky='w', padx=6, pady=2)
                    cb.bind('<<ComboboxSelected>>', lambda e: self.save_current())
                    self.tex_rows[s['texture']] = (cb, var, s)
                self.restore_mesh(mesh)
            self.images = list_images(self.ws.get())
            ws, game = self.ws.get(), self.game.get()
            self.run(lambda: job_texture_slots(ws, game, pkg, mesh, self.log), done)

        def restore_mesh(self, mesh):
            """Show the saved .glb and textures of `mesh`; a mesh never set up starts with no .glb (the main mesh
            starts with the first .glb in the character folder, as before)."""
            saved = self.choices()['meshes'].get(mesh)
            models = list(self.c_model_cb['values'])
            self.cur_mesh = mesh
            if saved is not None:
                self.c_model.set(saved['model'] if saved['model'] in models else NO_MODEL)
                for tex, (cb, var, s) in self.tex_rows.items():
                    value = saved['textures'].get(tex, KEEP)
                    var.set(value if value in cb['values'] else KEEP)
            else:
                first = next((m for m in models if m != NO_MODEL), None)
                self.c_model.set(first if mesh == self.c_default and first else NO_MODEL)
                self.auto_textures()
            self.save_current()

        def model_chosen(self):
            self.auto_textures()
            self.save_current()

        def pick_layer(self, var, label):
            path = filedialog.askopenfilename(title='Image for ' + label, filetypes=[('Images', ' '.join('*' + e for e in IMAGE_EXT))])
            if path:
                var.set(os.path.normpath(path))

        def do_make_pmsk(self):
            if not self.need_ready():
                return
            name = self.m_name.get().strip() or os.path.splitext(self.c_model.get().replace(NO_MODEL, ''))[0]
            if not name:
                messagebox.showwarning(APP, "Enter a name. Use your .glb's name so tab 2 picks the Pmsk automatically."); return
            self.m_name.set(name)
            layers = {k: v.get() for k, v in self.m_layers.items() if v.get()}
            raw = self.m_mode.get() == 'Raw channels'
            ws, game = self.ws.get(), self.game.get()

            def done(path):
                self.refresh_files()
                messagebox.showinfo(APP, 'Pmsk saved:\n%s\n\nIn tab 2, pick it for the Pmsk texture.' % path)
            self.run(lambda: job_make_pmsk(ws, game, name, layers, self.log, raw=raw), done)

        def do_extract(self):
            if not self.need_ready():
                return
            pkg, mesh = self.x_pkg.get(), self.x_mesh.get()
            if not pkg or not mesh:
                messagebox.showwarning(APP, 'Pick a character package and a mesh first.'); return
            ws, game = self.ws.get(), self.game.get()

            def done(outdir):
                self.open_dir(outdir)
                messagebox.showinfo(APP, 'Pmsk extracted to:\n%s\n\nStart with pmsk_report.txt.' % outdir)
            self.run(lambda: job_extract_pmsk(ws, game, pkg, mesh, self.log), done)

        def auto_textures(self, only_unchosen=False):
            model = self.c_model.get()
            if not self.tex_rows or model in ('', NO_MODEL):
                return
            if self.cur_mesh == self.c_default or not self.c_out.get():
                self.c_out.set(os.path.splitext(model)[0])
            choice = guess_textures([s for (_, _, s) in self.tex_rows.values()], getattr(self, 'images', []), model, self.ws.get())
            for tex, (cb, var, s) in self.tex_rows.items():
                if not only_unchosen or var.get() == KEEP:
                    var.set(choice.get(tex, KEEP))

        # ---------------------------------------------------------------- actions
        def do_export(self):
            if not self.need_ready():
                return
            pkg, mesh = self.e_pkg.get(), self.e_mesh.get()
            if not pkg or not mesh:
                messagebox.showwarning(APP, 'Pick a character package and a mesh first.'); return
            ws, game, tex, embed = self.ws.get(), self.game.get(), self.e_tex.get(), self.e_embed.get()
            blend, blender = self.e_blend.get(), self.settings.get('blender_exe', '')
            self.settings['make_blend'] = blend; self.save_settings()
            objects = [p for p, v in self.e_obj_vars.items() if v.get()]
            self.run(lambda: job_export_vanilla(ws, game, pkg, mesh, tex, embed, blend, blender, objects, self.log),
                     lambda outdir: messagebox.showinfo(APP, 'Exported to:\n%s' % outdir))

        def fill_checks(self, frame, objects=None, note='', ticked=lambda o: True, command=None):
            """One tick box per object (three per row); returns {object path: BooleanVar}."""
            for w in frame.winfo_children(): w.destroy()
            if note or not objects:
                ttk.Label(frame, text=note or 'This character has no extra objects.').grid(row=0, column=0, sticky='w')
                return {}
            out = {}
            for i, o in enumerate(objects):
                var = tk.BooleanVar(value=ticked(o))
                ttk.Checkbutton(frame, text=o['label'], variable=var, command=command).grid(row=i // 3, column=i % 3, sticky='w', padx=4)
                out[o['path']] = var
            return out

        def show_objects(self, objects=None, note=''):
            hidden = set(self.choices()['hidden']) if objects and self.c_pkg.get() else set()
            self.obj_vars = self.fill_checks(self.objframe, objects, note, ticked=lambda o: o['path'] not in hidden,
                                             command=self.save_current)
            self.update_summary()

        def load_export_objects(self):
            pkg, mesh = self.e_pkg.get(), self.e_mesh.get()
            if not pkg or not mesh:
                return
            self.e_obj_vars = self.fill_checks(self.e_objframe, note='Looking for extra objects...')
            ws, game = self.ws.get(), self.game.get()

            def done(objects):
                self.e_obj_vars = self.fill_checks(self.e_objframe, objects, ticked=lambda o: False)
            self.run(lambda: job_find_objects(ws, game, pkg, mesh, self.log), done)

        def do_convert(self):
            if not self.need_ready():
                return
            pkg = self.c_pkg.get()
            if not pkg or not self.c_paths:
                messagebox.showwarning(APP, 'Pick the base character first.'); return
            self.save_current()
            st = self.choices()
            targets = set(self.c_paths.values())
            parts = [dict(mesh=p, model=m['model'], textures=m['textures']) for p, m in st['meshes'].items()
                     if m.get('model') and p in targets]
            hide = [p for p, v in self.obj_vars.items() if not v.get()]
            if not parts and not hide:
                messagebox.showwarning(APP, 'Link a .glb to at least one mesh: pick it in "Mesh to replace", then pick your .glb.\n'
                                       'Put your .glb in the character folder (Import GLB...) if it is not listed.'); return
            uv2 = next(k for k, v in UV2_CHOICES.items() if v == self.o_uv2.get())
            opts = dict(darken=self.o_darken.get(), uv2=uv2, keep_size=self.o_keep.get(), wrinkles_off=self.o_wrinkles.get(), force=self.o_force.get(),
                        preview=self.o_preview.get(), out_name=self.c_out.get(), hide=hide)
            ws, game = self.ws.get(), self.game.get()
            self.run(lambda: job_convert_parts(ws, game, pkg, parts, log=self.log, **opts),
                     lambda outdir: messagebox.showinfo(APP, 'Converted! Files are in:\n%s' % outdir))

    os.makedirs(HERE / 'projects', exist_ok=True)
    app = Studio()
    test_errors = []
    if selftest:
        def check_widgets():
            if app.busy:
                app.after(100, check_widgets); return
            try:
                assert app.helpmenu.entrycget(0, 'label') == 'About %s' % APP
                before = str(app.c_pkg_cb.cget('state'))
                app.set_busy(True)
                assert str(app.c_pkg_cb.cget('state')) == 'disabled'
                app.set_busy(False)
                assert str(app.c_pkg_cb.cget('state')) == before
                for widget in app.winfo_children():
                    if isinstance(widget, ttk.Notebook):
                        for tab in widget.tabs():
                            widget.select(tab); app.update_idletasks()
                            print('GUI tab:', widget.tab(tab, 'text'), 'requested width:', widget.winfo_reqwidth(), 'available:', app.winfo_width())
                assert [k for k, _, _ in PMSK_LAYERS] == list(app.m_layers) and app.x_pkg_cb.winfo_exists()
                hat = dict(path='Characters.CHAR.X.Meshes.Hat', label='Hat  (variation 1)')
                app.show_objects([hat])
                assert list(app.obj_vars) == [hat['path']] and app.obj_vars[hat['path']].get()
                app.show_objects(note='Pick a base character first.')
                app.e_obj_vars = app.fill_checks(app.e_objframe, [hat], ticked=lambda o: False)
                assert not app.e_obj_vars[hat['path']].get()           # exporting objects is opt-in
                app.m_mode.set('Raw channels'); app.update_pmsk_mode()
                assert app.m_labels['wounds'].cget('text') == 'Red channel'
                app.m_mode.set('Body editing masks'); app.update_pmsk_mode()
                assert app.m_labels['wounds'].cget('text') == 'Wound mask (red)'
                print('GUI SELFTEST OK: controls lock/restore, all tabs constructed.')
            except Exception as exc:
                test_errors.append(exc)
            finally:
                app.destroy()
        app.after(1500, check_widgets)
    app.mainloop()
    if test_errors:
        raise test_errors[0]
    return True


if __name__ == '__main__':
    ok = run_gui(selftest='--selftest' in sys.argv)
    if '--selftest' in sys.argv:
        print('SELFTEST OK' if ok else 'SELFTEST FAILED')
