"""Owner setup for Telegram reports. Verification never sends a message."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import re
import sys
import tempfile
import threading

from local_web.report_telegram_config import (load_telegram_settings, safe_settings_status, save_telegram_settings,
                                             validate_bot_settings, validate_chat_id, validate_telegram_settings)

_STATES = {'awaiting_input', 'discovering', 'checking', 'chat_selected', 'saved', 'saved_unverified', 'error', 'closed'}
_ERRORS = {'authentication_failed', 'chat_unavailable', 'connection_failed', 'updates_unavailable',
           'rate_limited', 'save_failed', 'setup_failed', 'configuration_unavailable'}


def _select_entry(entry):
    entry.selection_range(0, 'end')
    entry.icursor('end')


def _entry_shortcut(event):
    """Windows physical keys work with Russian and Latin keyboard layouts."""
    state = getattr(event, 'state', 0)
    code = getattr(event, 'keycode', 0)
    key = str(getattr(event, 'keysym', '')).lower()
    windows = sys.platform == 'win32'
    if state & 0x4 and not state & 0x8:
        if key == 'v' or windows and code == 86:
            event.widget.event_generate('<<Paste>>')
            return 'break'  # Prevent a second paste from the Tk class binding.
        if key == 'a' or windows and code == 65:
            _select_entry(event.widget)
            return 'break'
    if state & 0x1 and not state & 0x4 and (key == 'insert' or windows and code == 45):
        event.widget.event_generate('<<Paste>>')
        return 'break'
    return None


def _paste_clipboard(entry):
    # Called only by the owner's Paste button/menu, never during setup/import.
    from tkinter import TclError
    if str(entry.cget('state')) == 'disabled':
        return
    try:
        text = entry.clipboard_get()
    except TclError:
        return
    try:
        entry.delete('sel.first', 'sel.last')
    except TclError:
        pass
    entry.insert('insert', text)
    entry.focus_set()


def install_entry_actions(entry, *, secret=False):
    import tkinter as tk
    entry.bind('<KeyPress>', _entry_shortcut)
    menu = tk.Menu(entry, tearoff=False)
    menu.add_command(label='Вставить', command=lambda: _paste_clipboard(entry))
    menu.add_command(label='Выделить всё', command=lambda: _select_entry(entry))
    if not secret:
        menu.add_command(label='Копировать', command=lambda: entry.event_generate('<<Copy>>'))

    def popup(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    entry.bind('<Button-3>', popup)


def write_status(path, *, configured, state, error_code=None):
    if path is None:
        return
    record = {'configured': bool(configured), 'state': state if state in _STATES else 'error',
              'created_at': datetime.now(timezone.utc).isoformat()}
    if error_code in _ERRORS:
        record['error_code'] = error_code
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Нельзя сохранить состояние по символической ссылке')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='telegram-status.', suffix='.tmp',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, ensure_ascii=False)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def setup_error_code(error):
    code = getattr(error, 'code', None)
    if code in ('telegram_http_401', 'telegram_api_401'):
        return 'authentication_failed'
    if code in ('telegram_http_400', 'telegram_api_400', 'telegram_http_403', 'telegram_api_403'):
        return 'chat_unavailable'
    if code in ('telegram_http_409', 'telegram_api_409'):
        return 'updates_unavailable'
    if code == 'telegram_rate_limited':
        return 'rate_limited'
    if isinstance(error, (OSError, TimeoutError)) or code == 'telegram_network_error' or code in {
            'telegram_http_500', 'telegram_http_502', 'telegram_http_503', 'telegram_http_504'}:
        return 'connection_failed'
    return 'setup_failed'


def chats_from_updates(updates):
    """Return explicit choices; never select the last or only observed chat."""
    chats = {}
    for update in updates if isinstance(updates, list) else []:
        if not isinstance(update, dict):
            continue
        records = [update.get(key) for key in ('message', 'edited_message', 'channel_post', 'edited_channel_post',
                                               'my_chat_member', 'chat_member', 'chat_join_request')]
        callback = update.get('callback_query')
        if isinstance(callback, dict):
            records.append(callback.get('message'))
        for record in records:
            chat = record.get('chat') if isinstance(record, dict) else None
            if not isinstance(chat, dict) or type(chat.get('id')) is not int:
                continue
            try:
                chat_id = validate_chat_id(chat['id'])
            except ValueError:
                continue
            name = chat.get('title') or ' '.join(str(chat.get(key) or '') for key in ('first_name', 'last_name')).strip()
            name = ''.join(c for c in str(name or 'Чат') if c.isprintable())[:100]
            kind = {'private': 'Личный чат', 'group': 'Группа', 'supergroup': 'Группа', 'channel': 'Канал'}.get(chat.get('type'), 'Чат')
            chats[chat_id] = {'chat_id': chat_id, 'label': f'{name} · {kind} · {chat_id}'}
    return sorted(chats.values(), key=lambda item: (item['label'].casefold(), item['chat_id']))


def verify_settings(settings):
    """Read-only getMe + getChat; return a pinned, verified numeric destination."""
    from local_web.telegram_transport import get_chat, get_me
    settings = validate_telegram_settings(settings)
    bot = get_me(settings)
    chat = get_chat(settings)
    if not isinstance(bot, dict) or bot.get('is_bot') is not True or type(bot.get('id')) is not int:
        raise ValueError('Не удалось подтвердить бота Telegram')
    if not isinstance(chat, dict) or type(chat.get('id')) is not int or str(chat['id']) != settings['chat_id']:
        raise ValueError('Не удалось подтвердить выбранный чат Telegram')
    username = bot.get('username')
    username = username if isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]{5,32}', username) else None
    return settings, username


def save_unverified_settings(settings, config_path=None, status_file=None):
    save_telegram_settings(validate_telegram_settings(settings), config_path)
    write_status(status_file, configured=True, state='saved_unverified')


def show_setup(status_file=None, config_path=None):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title('Настройка Telegram-бота · Регион')
    root.geometry('630x740')
    root.minsize(590, 720)
    root.configure(background='#f2f5fa')
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('TFrame', background='#f2f5fa')
    style.configure('TLabel', background='#f2f5fa', foreground='#17243d', font=('Segoe UI', 10))
    style.configure('Heading.TLabel', foreground='#0d2782', font=('Segoe UI', 18, 'bold'))
    style.configure('Muted.TLabel', foreground='#59677a')
    style.configure('TEntry', padding=8, font=('Segoe UI', 11))
    style.configure('TButton', padding=(12, 9), font=('Segoe UI', 10))
    style.configure('Primary.TButton', background='#3371dc', foreground='white')
    style.map('Primary.TButton', background=[('active', '#245fc5'), ('disabled', '#dbe3ef')], foreground=[('disabled', '#59677a')])
    outer = ttk.Frame(root, padding=(28, 22))
    outer.pack(fill='both', expand=True)
    outer.columnconfigure(0, weight=1)
    ttk.Label(outer, text='Отчёты в Telegram', style='Heading.TLabel').grid(row=0, column=0, sticky='w')
    ttk.Label(outer, text='Настройка владельца приложения', style='Muted.TLabel').grid(row=1, column=0, sticky='w', pady=(5, 17))
    ttk.Label(outer, text='1. Создайте бота у @BotFather командой /newbot.\n'
                         '2. Откройте своего бота и отправьте /start.\n'
                         '3. Введите токен и выберите чат для отчётов.',
              wraplength=560).grid(row=2, column=0, sticky='w', pady=(0, 18))
    ttk.Label(outer, text='Токен бота').grid(row=3, column=0, sticky='w')
    secret = tk.StringVar()
    token_row = ttk.Frame(outer)
    token_row.grid(row=4, column=0, sticky='ew', pady=(6, 6))
    token_row.columnconfigure(0, weight=1)
    token_entry = ttk.Entry(token_row, textvariable=secret, show='•')
    token_entry.grid(row=0, column=0, sticky='ew', padx=(0, 10))
    paste_button = ttk.Button(token_row, text='Вставить', command=lambda: _paste_clipboard(token_entry))
    paste_button.grid(row=0, column=1)
    token_help = tk.StringVar(value='Скопируйте токен из сообщения @BotFather.')
    ttk.Label(outer, textvariable=token_help, style='Muted.TLabel', wraplength=560).grid(row=5, column=0, sticky='w', pady=(0, 16))
    ttk.Label(outer, text='ID чата').grid(row=6, column=0, sticky='w')
    chat_row = ttk.Frame(outer)
    chat_row.grid(row=7, column=0, sticky='ew', pady=(6, 6))
    chat_row.columnconfigure(0, weight=1)
    chat_id = tk.StringVar()
    chat_entry = ttk.Entry(chat_row, textvariable=chat_id)
    chat_entry.grid(row=0, column=0, sticky='ew', padx=(0, 10))
    find_button = ttk.Button(chat_row, text='Найти чат')
    find_button.grid(row=0, column=1)
    ttk.Label(outer, text='Найдите чат по сообщениям боту или укажите числовой ID вручную.',
              style='Muted.TLabel', wraplength=560).grid(row=8, column=0, sticky='w', pady=(0, 15))
    ttk.Label(outer, text='ID темы · необязательно').grid(row=9, column=0, sticky='w')
    thread_id = tk.StringVar()
    thread_entry = ttk.Entry(outer, textvariable=thread_id)
    thread_entry.grid(row=10, column=0, sticky='ew', pady=(6, 6))
    ttk.Label(outer, text='Оставьте пустым для отправки в основной чат.', style='Muted.TLabel').grid(row=11, column=0, sticky='w', pady=(0, 17))
    ttk.Label(outer, text='Токен хранится зашифрованным для текущего пользователя Windows. '
                         'Проверка подтверждает бота и чат, сообщения не отправляются.',
              style='Muted.TLabel', wraplength=560).grid(row=12, column=0, sticky='w', pady=(0, 13))
    message = tk.StringVar(value='Введите токен, затем найдите чат для отчётов.')
    ttk.Label(outer, textvariable=message, wraplength=560).grid(row=13, column=0, sticky='w', pady=(0, 15))
    actions = ttk.Frame(outer)
    actions.grid(row=14, column=0, sticky='ew')
    save_button = ttk.Button(actions, text='Проверить и сохранить', style='Primary.TButton')
    save_button.grid(row=0, column=0, sticky='w')
    later_button = ttk.Button(actions, text='Сохранить без проверки')
    later_button.grid(row=1, column=0, sticky='w', pady=(9, 0))
    later_button.grid_remove()
    install_entry_actions(token_entry, secret=True)
    install_entry_actions(chat_entry)
    install_entry_actions(thread_entry)

    state = {'configured': False, 'busy': False, 'saved': None, 'deferred': None}
    closed = threading.Event()
    completed = queue.Queue()
    try:
        saved = load_telegram_settings(config_path)
        if saved:
            state.update(configured=True, saved=saved)
            chat_id.set(saved['chat_id'])
            thread_id.set(str(saved.get('message_thread_id', '')))
            token_help.set('Токен сохранён. Для замены введите новый; иначе оставьте поле пустым.')
            message.set('Telegram уже настроен. Можно изменить чат или проверить соединение.')
    except (OSError, ValueError):
        message.set('Сохранённая настройка недоступна. Введите токен заново.')
    write_status(status_file, configured=state['configured'], state='awaiting_input')

    def read_settings(require_chat=True):
        token = secret.get().strip() or (state['saved'] or {}).get('bot_token', '')
        settings = {'bot_token': token, 'chat_id': chat_id.get().strip(),
                    'message_thread_id': thread_id.get().strip(), 'timeout': 20}
        return validate_telegram_settings(settings) if require_chat else validate_bot_settings(settings)

    def busy(value):
        state['busy'] = value
        for widget in (token_entry, chat_entry, thread_entry, paste_button, find_button, save_button):
            widget.configure(state='disabled' if value else 'normal')

    def choose_chat(chats):
        if not chats:
            message.set('Чаты пока не найдены. Отправьте боту /start в Telegram и нажмите «Найти чат» ещё раз.')
            return
        dialog = tk.Toplevel(root)
        dialog.title('Выберите чат для отчётов')
        dialog.geometry('640x340')
        dialog.minsize(540, 300)
        dialog.configure(background='#f2f5fa')
        dialog.transient(root)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text='Выберите получателя отчётов. Чат не выбирается автоматически.', wraplength=580).grid(row=0, column=0, sticky='w', pady=(0, 12))
        listing = tk.Listbox(frame, exportselection=False, activestyle='dotbox', font=('Segoe UI', 10),
                             background='white', foreground='#17243d', selectbackground='#3371dc', selectforeground='white')
        listing.grid(row=1, column=0, sticky='nsew')
        for chat in chats:
            listing.insert('end', chat['label'])
        scroll = ttk.Scrollbar(frame, orient='vertical', command=listing.yview)
        scroll.grid(row=1, column=1, sticky='ns')
        listing.configure(yscrollcommand=scroll.set)
        select = ttk.Button(frame, text='Выбрать чат', style='Primary.TButton', state='disabled')
        select.grid(row=2, column=0, sticky='w', pady=(14, 0))

        def pick(event=None):
            selection = listing.curselection()
            if not selection:
                return
            chat_id.set(chats[selection[0]]['chat_id'])
            message.set('Чат выбран. Нажмите «Проверить и сохранить», чтобы завершить настройку.')
            write_status(status_file, configured=state['configured'], state='chat_selected')
            dialog.destroy()

        listing.bind('<<ListboxSelect>>', lambda event: select.configure(state='normal' if listing.curselection() else 'disabled'))
        listing.bind('<Double-Button-1>', pick)
        dialog.bind('<Return>', pick)
        select.configure(command=pick)
        dialog.grab_set()
        listing.focus_set()

    def failure(code):
        messages = {
            'authentication_failed': 'Telegram не принял токен. Скопируйте его заново у @BotFather.',
            'chat_unavailable': 'Бот не видит выбранный чат. Отправьте ему /start или проверьте ID чата.',
            'updates_unavailable': 'У бота уже работает другое подключение. Укажите ID чата вручную.',
            'connection_failed': 'Не удалось подключиться к Telegram. Повторите проверку или сохраните настройку для отправки позже.',
            'rate_limited': 'Telegram просит подождать. Повторите проверку позже.',
            'save_failed': 'Не удалось сохранить защищённую настройку в Windows. Повторите попытку.',
        }
        message.set(messages.get(code, 'Не удалось завершить проверку. Проверьте токен и выбранный чат.'))
        write_status(status_file, configured=state['configured'], state='error', error_code=code)
        if code == 'connection_failed' and state['deferred']:
            later_button.grid()
        else:
            state['deferred'] = None

    def worker(kind, settings):
        try:
            if kind == 'find':
                from local_web.telegram_transport import get_updates
                value = chats_from_updates(get_updates(settings))
            else:
                value = verify_settings(settings)
            completed.put((kind, value, None))
        except Exception as error:
            completed.put((kind, None, setup_error_code(error)))

    def start(kind):
        if state['busy']:
            return
        try:
            settings = read_settings(require_chat=kind != 'find')
        except ValueError as error:
            message.set(str(error))
            return
        later_button.grid_remove()
        state['deferred'] = settings if kind == 'save' else None
        busy(True)
        message.set('Ищем чаты по сообщениям боту…' if kind == 'find' else 'Проверяем бота и доступ к выбранному чату…')
        write_status(status_file, configured=state['configured'], state='discovering' if kind == 'find' else 'checking')
        threading.Thread(target=worker, args=(kind, settings), daemon=True).start()

    def finish_save(settings, verified=True, username=None):
        try:
            if verified:
                save_telegram_settings(settings, config_path)
                write_status(status_file, configured=True, state='saved')
            else:
                save_unverified_settings(settings, config_path, status_file)
        except (ValueError, OSError):
            failure('save_failed')
            return
        state.update(configured=True, saved=settings, deferred=None)
        secret.set('')
        later_button.grid_remove()
        token_help.set('Токен сохранён. Для замены введите новый; иначе оставьте поле пустым.')
        message.set(('Бот' + (' @' + username if username else '') + ' и чат проверены. Настройка сохранена.') if verified else
                    'Настройка сохранена без проверки. Отчёты отправятся, когда Telegram станет доступен.')

    def poll():
        if closed.is_set():
            return
        try:
            kind, value, error = completed.get_nowait()
        except queue.Empty:
            pass
        else:
            busy(False)
            if error:
                failure(error)
            elif kind == 'find':
                write_status(status_file, configured=state['configured'], state='awaiting_input')
                choose_chat(value)
            else:
                finish_save(value[0], username=value[1])
        root.after(100, poll)

    def invalidate(*args):
        state['deferred'] = None
        later_button.grid_remove()

    for variable in (secret, chat_id, thread_id):
        variable.trace_add('write', invalidate)
    find_button.configure(command=lambda: start('find'))
    save_button.configure(command=lambda: start('save'))
    later_button.configure(command=lambda: finish_save(state['deferred'], verified=False) if state['deferred'] else None)
    root.bind('<Return>', lambda event: start('save'))

    def close():
        closed.set()
        state.update(saved=None, deferred=None)
        secret.set('')
        write_status(status_file, configured=state['configured'], state='closed')
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', close)
    token_entry.focus_set()
    root.after(100, poll)
    root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Owner-only Telegram report setup; verification sends no messages')
    parser.add_argument('--status-file', type=Path)
    parser.add_argument('--config-path', type=Path)
    parser.add_argument('--check-config', action='store_true')
    args = parser.parse_args(argv)
    if args.check_config:
        try:
            record = safe_settings_status(args.config_path)
        except (ValueError, OSError):
            record = {'configured': False, 'error_code': 'configuration_unavailable'}
        print(json.dumps(record, ensure_ascii=False))
        return 0 if record.get('configured') else 1
    show_setup(args.status_file, args.config_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
