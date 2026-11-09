"""Owner Telegram configuration. The bot token is stored only with user DPAPI.

The former SMTP configuration is never imported, read, migrated or removed.
Environment credentials work on all platforms. Import performs no IO/network.
"""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import re
import tempfile

if __package__:
    from .secure_config import protect_secret
else:
    from secure_config import protect_secret

_ENTROPY = b'RegionDocumentConverter.ReportTelegram.v1'
_DESCRIPTION = 'Region Document Converter Telegram reports'
_TOKEN = re.compile(r'[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,230}')
_CHAT = re.compile(r'-?[1-9][0-9]{0,18}')


def default_config_path(environ=None):
    env = os.environ if environ is None else environ
    local = env.get('LOCALAPPDATA')
    folder = Path(local) if local else Path.home() / '.config'
    return folder / 'RegionDocumentConverter' / 'report-telegram.json'


def validate_bot_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError('Некорректная настройка Telegram')
    token = settings.get('bot_token')
    if not isinstance(token, str) or not _TOKEN.fullmatch(token) or len(token) > 256:
        raise ValueError('Введите токен бота, полученный у @BotFather')
    try:
        if isinstance(settings.get('timeout'), bool):
            raise ValueError
        timeout = float(settings.get('timeout', 20))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Некорректное время ожидания Telegram') from None
    if not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise ValueError('Некорректное время ожидания Telegram')
    return {'bot_token': token, 'timeout': timeout}


def validate_telegram_settings(settings):
    result = validate_bot_settings(settings)
    result['chat_id'] = validate_chat_id(settings.get('chat_id'))
    thread = settings.get('message_thread_id')
    if thread not in (None, ''):
        if type(thread) not in (str, int) or not re.fullmatch(r'[1-9][0-9]{0,9}', str(thread)) or int(thread) >= 2**31:
            raise ValueError('ID темы должен быть положительным целым числом')
        result['message_thread_id'] = int(thread)
    return result


def validate_chat_id(value):
    chat = str(value) if type(value) in (str, int) else ''
    if not _CHAT.fullmatch(chat) or not -(2**63) <= int(chat) < 2**63:
        raise ValueError('Выберите чат или введите его числовой ID')
    return chat


def _protect(value, *, decrypt=False):
    return protect_secret(value, entropy=_ENTROPY, description=_DESCRIPTION, decrypt=decrypt)


def _read_record(path):
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError('Некорректный файл настройки Telegram')
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('Не удалось прочитать настройку Telegram') from None
    if not isinstance(record, dict) or record.get('schema') != 1 or record.get('protection') != 'windows-dpapi-current-user':
        raise ValueError('Неподдерживаемый формат настройки Telegram')
    if 'bot_token' in record or 'token' in record:
        raise ValueError('Незашифрованный токен в настройке Telegram запрещён')
    return record


def load_telegram_settings(config_path=None, environ=None):
    env = os.environ if environ is None else environ
    mapping = {'BOT_TOKEN': 'bot_token', 'CHAT_ID': 'chat_id', 'MESSAGE_THREAD_ID': 'message_thread_id', 'TIMEOUT': 'timeout'}
    overrides = {name: env['REGION_REPORT_TELEGRAM_' + suffix] for suffix, name in mapping.items()
                 if 'REGION_REPORT_TELEGRAM_' + suffix in env}
    # A complete environment configuration is independent of local profile access.
    if 'bot_token' in overrides and 'chat_id' in overrides:
        return validate_telegram_settings(overrides)
    path = Path(config_path) if config_path is not None else default_config_path(env)
    record = _read_record(path)
    settings = {key: record[key] for key in ('chat_id', 'timeout', 'message_thread_id') if key in record}
    settings.update(overrides)
    if 'bot_token' not in settings and record:
        encrypted = record.get('bot_token_dpapi')
        if not isinstance(encrypted, str) or not encrypted:
            raise ValueError('В настройке Telegram отсутствует защищённый токен')
        try:
            settings['bot_token'] = _protect(base64.b64decode(encrypted, validate=True), decrypt=True).decode('utf-8')
        except (ValueError, UnicodeError):
            raise ValueError('Сохранённый токен недоступен текущему пользователю Windows') from None
    if not record and not any(key in settings for key in ('bot_token', 'chat_id')):
        return None
    return validate_telegram_settings(settings)


def save_telegram_settings(settings, config_path=None):
    settings = validate_telegram_settings(settings)
    protected = _protect(settings['bot_token'].encode('utf-8'))
    record = {key: value for key, value in settings.items() if key != 'bot_token'}
    record.update(schema=1, protection='windows-dpapi-current-user',
                  bot_token_dpapi=base64.b64encode(protected).decode('ascii'))
    path = Path(config_path) if config_path is not None else default_config_path()
    if path.is_symlink():
        raise ValueError('Нельзя сохранить настройку по символической ссылке')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', prefix='report-telegram.', suffix='.tmp',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path


def safe_settings_status(config_path=None, environ=None):
    return {'configured': load_telegram_settings(config_path, environ) is not None}
