"""Library to interact with the Simplepush notification service."""
import base64
import os
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import hashlib
import requests
import json
import aiohttp
import asyncio
import time

DEFAULT_TIMEOUT = 5

SALT = '1789F0B8C4A051E5'

SIMPLEPUSH_URL = 'https://api.simplepush.io'


class BadRequest(Exception):
    """Raised when API thinks that title or message are too long."""
    pass

class UnknownError(Exception):
    """Raised for invalid responses."""
    pass

class FeedbackActionError(Exception):
    """Raised when feedback API is not reachable."""
    pass

class FeedbackActionTimeout(Exception):
    """Raised when a feedback action timed out."""
    pass


def send(key, message, title=None, event=None, actions=None, feedback_callback=None, feedback_callback_timeout=60):
    """Send a plain-text message."""
    if not key or not message:
        raise ValueError("Key and message argument must be set")

    _check_actions(actions)

    payload = _generate_payload(key, title, message, event, actions, None, None)

    r = requests.post(SIMPLEPUSH_URL + '/send', json=payload, timeout=DEFAULT_TIMEOUT)
    asyncio.run(_handle_response(r, feedback_callback, feedback_callback_timeout))

def send_encrypted(key, password, salt, message, title=None, event=None, actions=None, feedback_callback=None, feedback_callback_timeout=60):
    """Send an encrypted message."""
    if not key or not message or not password:
        raise ValueError("Key, message and password arguments must be set")

    _check_actions(actions)

    payload = _generate_payload(key, title, message, event, actions, password, salt)

    r = requests.post(SIMPLEPUSH_URL + '/send', json=payload, timeout=DEFAULT_TIMEOUT)
    asyncio.run(_handle_response(r, feedback_callback, feedback_callback_timeout))

async def async_send(key, message, title=None, event=None, actions=None, feedback_callback=None, feedback_callback_timeout=60):
    """Send a plain-text message."""
    if not key or not message:
        raise ValueError("Key and message argument must be set")

    _check_actions(actions)

    payload = _generate_payload(key, title, message, event, actions, None, None)

    async with aiohttp.ClientSession(raise_for_status=True) as session:
        async with session.post(SIMPLEPUSH_URL + '/send', json=payload) as resp:
            return await _handle_response_aio(await resp.json(), feedback_callback, feedback_callback_timeout)

async def async_send_encrypted(key, password, salt, message, title=None, event=None, actions=None, feedback_callback=None, feedback_callback_timeout=60):
    """Send an encrypted message."""
    if not key or not message or not password:
        raise ValueError("Key, message and password arguments must be set")

    _check_actions(actions)

    payload = _generate_payload(key, title, message, event, actions, password, salt)

    async with aiohttp.ClientSession(raise_for_status=True) as session:
        async with session.post(SIMPLEPUSH_URL + '/send', json=payload) as resp:
            return await _handle_response_aio(await resp.json(), feedback_callback, feedback_callback_timeout)

async def _handle_response(response, feedback_callback, feedback_callback_timeout):
    """Raise error if message was not successfully sent."""
    if response.json()['status'] == 'BadRequest' and response.json()['message'] == 'Title or message too long':
        raise BadRequest

    if response.json()['status'] != 'OK':
        raise UnknownError

    if 'feedbackId' in response.json() and feedback_callback is not None:
        feedback_id = response.json()['feedbackId']
        await _query_feedback_endpoint(feedback_id, feedback_callback, feedback_callback_timeout)

    response.raise_for_status()

async def _handle_response_aio(json_response, feedback_callback, feedback_callback_timeout):
    """Raise error if message was not successfully sent."""
    if json_response['status'] == 'BadRequest' and json_response['message'] == 'Title or message too long':
        raise BadRequest

    if json_response['status'] != 'OK':
        raise UnknownError

    if 'feedbackId' in json_response and feedback_callback is not None:
        feedback_id = json_response['feedbackId']
        await _query_feedback_endpoint(feedback_id, feedback_callback, feedback_callback_timeout)

def _generate_payload(key, title, message, event=None, actions=None, password=None, salt=None):
    """Generator for the payload."""
    payload = {'key': key}

    if not password:
        payload.update({'msg': message})

        if title:
            payload.update({'title': title})

        if event:
            payload.update({'event': event})
    else:
        encryption_key = _generate_encryption_key(password, salt)
        iv = _generate_iv()
        iv_hex = ""
        for c_idx in range(len(iv)):
            iv_hex += "{:02x}".format(ord(iv[c_idx:c_idx+1]))
        iv_hex = iv_hex.upper()

        payload.update({'encrypted': 'true', 'iv': iv_hex})

        if title:
            title = _encrypt(encryption_key, iv, title)
            payload.update({'title': title})

        if event:
            payload.update({'event': event})

        message = _encrypt(encryption_key, iv, message)
        payload.update({'msg': message})

    if actions:
        payload.update({'actions': actions})

    return payload


def _generate_iv():
    """Generator for the initialization vector."""
    return os.urandom(algorithms.AES.block_size // 8)


def _generate_encryption_key(password, salt=None):
    """Create the encryption key."""
    if salt:
        salted_password = password + salt
    else:
        # Compatibility for older versions
        salted_password = password + SALT
    hex_str = hashlib.sha1(salted_password.encode('utf-8')).hexdigest()[0:32]
    byte_str = bytearray.fromhex(hex_str)
    return bytes(byte_str)


def _encrypt(encryption_key, iv, data):
    """Encrypt the payload."""
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    data = padder.update(data.encode()) + padder.finalize()

    encryptor = Cipher(algorithms.AES(encryption_key), modes.CBC(iv), default_backend()).encryptor()
    return base64.urlsafe_b64encode(encryptor.update(data) + encryptor.finalize()).decode('ascii')


def _check_actions(actions):
    """Raise error if actions can't be parsed"""
    if not isinstance(actions, list) and actions is not None:
        raise ValueError("Actions malformed")

    if isinstance(actions, list) and len(actions) > 0:
        if isinstance(actions[0], str):
            if not all(isinstance(el, str) for el in actions):
                raise ValueError("Feedback actions malformed")
        else:
            if not all('name' in el.keys() and 'url' in el.keys() for el in actions):
                raise ValueError("Get actions malformed")


async def _query_feedback_endpoint(feedback_id, callback, timeout):
    stop = False
    n = 0
    start = time.time()

    async with aiohttp.ClientSession() as session:
        while not stop:
            async with session.get(SIMPLEPUSH_URL + '/1/feedback/' + feedback_id) as resp:
                json = await resp.json()
                if resp.ok and json['success']:
                    if json['action_selected']:
                        stop = True

                        callback(json['action_selected'], json['action_selected_at'], json['action_delivered_at'], feedback_id)
                    else:
                        if timeout:
                            now = time.time()
                            if now > start + timeout:
                                stop = True
                                raise FeedbackActionTimeout("Feedback Action ID: " + feedback_id)

                        if n < 60:
                            # In the first minute query every second
                            await asyncio.sleep(1)
                        elif n < 260:
                            # In the ten minutes after the first minute query every 3 seconds
                            await asyncio.sleep(3)
                        else:
                            # After 11 minutes query every five seconds
                            await asyncio.sleep(5)
                else:
                    stop = True
                    raise FeedbackActionError("Failed to reach feedback API.")
