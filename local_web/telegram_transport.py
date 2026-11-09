"""Small Telegram Bot API boundary. Importing this module performs no I/O."""
from __future__ import annotations

import http.client
import json
import math
from pathlib import Path
import re
import ssl
from urllib.parse import urlencode
import uuid

HOST = 'api.telegram.org'
MAX_DOCUMENT_BYTES = 50_000_000
_TOKEN = re.compile(r'([1-9][0-9]*):[A-Za-z0-9_-]{20,}')


class TelegramDeliveryError(Exception):
    """Only stable codes leave the boundary; response text/URL may contain secrets."""
    def __init__(self, code, retry_after=None):
        self.code = code if isinstance(code, str) and re.fullmatch(r'telegram_[a-z0-9_]+', code) else 'telegram_delivery_failed'
        self.retry_after = retry_after
        super().__init__(self.code)


def validate_transport_settings(settings, *, require_chat=True):
    try:
        if not isinstance(settings, dict):
            raise ValueError
        token = settings.get('bot_token')
        if not isinstance(token, str) or len(token) > 256 or not _TOKEN.fullmatch(token):
            raise ValueError
        timeout = float(settings.get('timeout', 30))
        if not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError
        result = dict(bot_token=token, timeout=timeout)
        if require_chat:
            chat = str(settings.get('chat_id', '')).strip()
            # A pinned numeric ID does not change when a username is reassigned.
            if not re.fullmatch(r'-?[1-9][0-9]*', chat):
                raise ValueError
            result['chat_id'] = chat
            if settings.get('message_thread_id') is not None:
                thread = settings['message_thread_id']
                if isinstance(thread, bool) or not re.fullmatch(r'[1-9][0-9]*', str(thread)):
                    raise ValueError
                result['message_thread_id'] = int(thread)
        return result
    except (ValueError, TypeError, OverflowError):
        raise TelegramDeliveryError('telegram_configuration_invalid') from None


def destination_key(settings):
    settings = validate_transport_settings(settings)
    import hashlib
    # The public bot ID survives token rotation; the token itself is never stored.
    identity = [settings['bot_token'].split(':', 1)[0], settings['chat_id'], settings.get('message_thread_id')]
    return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()


def _request(method, settings, fields, document=None):
    connection = None
    try:
        connection = http.client.HTTPSConnection(HOST, timeout=settings['timeout'], context=ssl.create_default_context())
        if document is None:
            body = urlencode(fields).encode('utf-8')
            connection.request('POST', '/bot' + settings['bot_token'] + '/' + method, body=body,
                               headers={'Content-Type': 'application/x-www-form-urlencoded'})
        else:
            origin = Path(document['path'])
            size = origin.stat().st_size
            if size > MAX_DOCUMENT_BYTES:
                raise TelegramDeliveryError('telegram_document_too_large')
            name = str(document.get('name', origin.name))
            if not re.fullmatch(r'[A-Za-z0-9._-]{1,180}', name):
                raise TelegramDeliveryError('telegram_document_name_invalid')
            boundary = 'RegionReport' + uuid.uuid4().hex
            prefix = b''.join((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode('utf-8')
                              for key, value in fields.items())
            prefix += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\n'
                       'Content-Type: application/octet-stream\r\n\r\n').encode('ascii')
            ending = f'\r\n--{boundary}--\r\n'.encode('ascii')
            connection.putrequest('POST', '/bot' + settings['bot_token'] + '/' + method)
            connection.putheader('Content-Type', 'multipart/form-data; boundary=' + boundary)
            connection.putheader('Content-Length', str(len(prefix) + size + len(ending)))
            connection.endheaders()
            connection.send(prefix)
            with origin.open('rb') as stream:
                for block in iter(lambda: stream.read(256 * 1024), b''):
                    connection.send(block)
            connection.send(ending)
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        try:
            data = json.loads(raw) if len(raw) <= 1024 * 1024 else None
        except (ValueError, UnicodeError):
            data = None
        if response.status == 429 or isinstance(data, dict) and data.get('error_code') == 429:
            parameters = data.get('parameters') if isinstance(data, dict) else None
            retry = parameters.get('retry_after') if isinstance(parameters, dict) else None
            retry = retry if type(retry) is int and retry > 0 else 30
            raise TelegramDeliveryError('telegram_rate_limited', retry_after=retry)
        if response.status != 200:
            raise TelegramDeliveryError('telegram_http_' + str(response.status))
        if not isinstance(data, dict) or data.get('ok') is not True:
            code = data.get('error_code') if isinstance(data, dict) else None
            raise TelegramDeliveryError('telegram_api_' + str(code) if type(code) is int else 'telegram_response_invalid')
        return data.get('result')
    except TelegramDeliveryError:
        raise
    except (OSError, http.client.HTTPException):
        raise TelegramDeliveryError('telegram_network_error') from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


def send_document(part, caption, settings):
    settings = validate_transport_settings(settings)
    if not isinstance(caption, str) or len(caption.encode('utf-16-le')) // 2 > 1024:
        raise TelegramDeliveryError('telegram_caption_invalid')
    fields = {'chat_id': settings['chat_id'], 'caption': caption, 'disable_content_type_detection': 'true'}
    if settings.get('message_thread_id') is not None:
        fields['message_thread_id'] = settings['message_thread_id']
    result = _request('sendDocument', settings, fields, part)
    if not isinstance(result, dict) or type(result.get('message_id')) is not int or result['message_id'] <= 0:
        raise TelegramDeliveryError('telegram_message_unconfirmed')
    return result['message_id']


def get_me(settings):
    result = _request('getMe', validate_transport_settings(settings, require_chat=False), {})
    if not isinstance(result, dict) or type(result.get('id')) is not int or result.get('is_bot') is not True:
        raise TelegramDeliveryError('telegram_response_invalid')
    return result


def get_chat(settings):
    settings = validate_transport_settings(settings)
    result = _request('getChat', settings, {'chat_id': settings['chat_id']})
    if not isinstance(result, dict) or type(result.get('id')) is not int:
        raise TelegramDeliveryError('telegram_response_invalid')
    return result


def get_updates(settings):
    result = _request('getUpdates', validate_transport_settings(settings, require_chat=False), {'timeout': 0, 'limit': 100})
    if not isinstance(result, list):
        raise TelegramDeliveryError('telegram_response_invalid')
    return result
