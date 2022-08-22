#!/usr/bin/env python3

import asyncio
import ipaddress
import json
import logging
import os
import socket

from datetime import datetime
from io import BufferedIOBase
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

from defang import refang  # type: ignore
from redis.asyncio import Redis
from playwrightcapture import Capture, PlaywrightCaptureException

from lookyloo.default import AbstractManager, get_config, get_socket_path, safe_create_dir
from lookyloo.helpers import get_captures_dir, load_cookies, UserAgents, ParsedUserAgent

from lookyloo.modules import FOX

logging.basicConfig(format='%(asctime)s %(name)s %(levelname)s:%(message)s',
                    level=logging.INFO)


class AsyncCapture(AbstractManager):

    def __init__(self, loglevel: int=logging.INFO):
        super().__init__(loglevel)
        self.script_name = 'async_capture'
        self.only_global_lookups: bool = get_config('generic', 'only_global_lookups')
        self.capture_dir: Path = get_captures_dir()
        self.user_agents = UserAgents()

        self.fox = FOX(get_config('modules', 'FOX'))
        if not self.fox.available:
            self.logger.warning('Unable to setup the FOX module')

    def thirdparty_submit(self, url: str) -> None:
        if self.fox.available:
            self.fox.capture_default_trigger(url, auto_trigger=True)

    async def process_capture_queue(self) -> None:
        '''Process a query from the capture queue'''
        value: List[Tuple[bytes, float]] = await self.redis.zpopmax('to_capture')
        if not value or not value[0]:
            # The queue was consumed by an other process.
            return
        uuid: str = value[0][0].decode()
        queue: Optional[bytes] = await self.redis.getdel(f'{uuid}_mgmt')
        await self.redis.sadd('ongoing', uuid)

        to_capture: Dict[bytes, bytes] = await self.redis.hgetall(uuid)

        if get_config('generic', 'default_public'):
            # By default, the captures are on the index, unless the user mark them as un-listed
            listing = False if (b'listing' in to_capture and to_capture[b'listing'].lower() in [b'false', b'0', b'']) else True
        else:
            # By default, the captures are not on the index, unless the user mark them as listed
            listing = True if (b'listing' in to_capture and to_capture[b'listing'].lower() in [b'true', b'1']) else False

        # Turn the freetext for the headers into a dict
        headers: Dict[str, str] = {}
        if b'headers' in to_capture:
            for header_line in to_capture[b'headers'].decode().splitlines():
                if header_line and ':' in header_line:
                    splitted = header_line.split(':', 1)
                    if splitted and len(splitted) == 2:
                        header, h_value = splitted
                        if header and h_value:
                            headers[header.strip()] = h_value.strip()
        if to_capture.get(b'dnt'):
            headers['DNT'] = to_capture[b'dnt'].decode()

        if to_capture.get(b'document'):
            # we do not have a URL yet.
            document_name = Path(to_capture[b'document_name'].decode()).name
            tmp_f = NamedTemporaryFile(suffix=document_name, delete=False)
            with open(tmp_f.name, "wb") as f:
                f.write(to_capture[b'document'])
            url = f'file://{tmp_f.name}'
        elif to_capture.get(b'url'):
            url = to_capture[b'url'].decode()
            self.thirdparty_submit(url)
        else:
            self.logger.warning(f'Invalid capture (no URL provided): {to_capture}.')
            url = ''

        if url:
            self.logger.info(f'Capturing {url} - {uuid}')
            success, error_message = await self._capture(
                url,
                perma_uuid=uuid,
                cookies_pseudofile=to_capture.get(b'cookies', None),
                listing=listing,
                user_agent=to_capture[b'user_agent'].decode() if to_capture.get(b'user_agent') else None,
                referer=to_capture[b'referer'].decode() if to_capture.get(b'referer') else None,
                headers=headers if headers else None,
                proxy=to_capture[b'proxy'].decode() if to_capture.get(b'proxy') else None,
                os=to_capture[b'os'].decode() if to_capture.get(b'os') else None,
                browser=to_capture[b'browser'].decode() if to_capture.get(b'browser') else None,
                browser_engine=to_capture[b'browser_engine'].decode() if to_capture.get(b'browser_engine') else None,
                device_name=to_capture[b'device_name'].decode() if to_capture.get(b'device_name') else None,
                parent=to_capture[b'parent'].decode() if to_capture.get(b'parent') else None
            )

            if to_capture.get(b'document'):
                os.unlink(tmp_f.name)

            if success:
                self.logger.info(f'Successfully captured {url} - {uuid}')
            else:
                self.logger.warning(f'Unable to capture {url} - {uuid}: {error_message}')
                await self.redis.setex(f'error_{uuid}', 36000, f'{error_message} - {url} - {uuid}')

        async with self.redis.pipeline() as lazy_cleanup:
            if queue and await self.redis.zscore('queues', queue):
                await lazy_cleanup.zincrby('queues', -1, queue)
            await lazy_cleanup.srem('ongoing', uuid)
            await lazy_cleanup.delete(uuid)
            # make sure to expire the key if nothing was processed for a while (= queues empty)
            await lazy_cleanup.expire('queues', 600)
            await lazy_cleanup.execute()

    async def _capture(self, url: str, *, perma_uuid: str,
                       cookies_pseudofile: Optional[Union[BufferedIOBase, str, bytes]]=None,
                       listing: bool=True, user_agent: Optional[str]=None,
                       referer: Optional[str]=None,
                       headers: Optional[Dict[str, str]]=None,
                       proxy: Optional[Union[str, Dict]]=None, os: Optional[str]=None,
                       browser: Optional[str]=None, parent: Optional[str]=None,
                       browser_engine: Optional[str]=None,
                       device_name: Optional[str]=None,
                       viewport: Optional[Dict[str, int]]=None) -> Tuple[bool, str]:
        '''Launch a capture'''
        url = url.strip()
        url = refang(url)
        if not url.startswith('data') and not url.startswith('http') and not url.startswith('file'):
            url = f'http://{url}'
        splitted_url = urlsplit(url)
        if self.only_global_lookups:
            if url.startswith('data') or url.startswith('file'):
                pass
            elif splitted_url.netloc:
                if splitted_url.hostname and splitted_url.hostname.split('.')[-1] != 'onion':
                    try:
                        ip = socket.gethostbyname(splitted_url.hostname)
                    except socket.gaierror:
                        self.logger.info('Name or service not known')
                        return False, 'Name or service not known.'
                    if not ipaddress.ip_address(ip).is_global:
                        return False, 'Capturing ressources on private IPs is disabled.'
            else:
                return False, 'Unable to find hostname or IP in the query.'

        # check if onion
        if (not proxy and splitted_url.netloc and splitted_url.hostname
                and splitted_url.hostname.split('.')[-1] == 'onion'):
            proxy = get_config('generic', 'tor_proxy')

        if not user_agent:
            # Catch case where the UA is broken on the UI, and the async submission.
            self.user_agents.user_agents  # triggers an update of the default UAs

        capture_ua = user_agent if user_agent else self.user_agents.default['useragent']
        if not browser_engine:
            # Automatically pick a browser
            parsed_ua = ParsedUserAgent(capture_ua)
            if not parsed_ua.browser:
                browser_engine = 'webkit'
            elif parsed_ua.browser.lower().startswith('chrom'):
                browser_engine = 'chromium'
            elif parsed_ua.browser.lower().startswith('firefox'):
                browser_engine = 'firefox'
            else:
                browser_engine = 'webkit'

        self.logger.info(f'Capturing {url}')
        try:
            async with Capture(browser=browser_engine, device_name=device_name, proxy=proxy) as capture:
                if headers:
                    capture.headers = headers
                if cookies_pseudofile:
                    # required by Mypy: https://github.com/python/mypy/issues/3004
                    capture.cookies = load_cookies(cookies_pseudofile)  # type: ignore
                if viewport:
                    # required by Mypy: https://github.com/python/mypy/issues/3004
                    capture.viewport = viewport  # type: ignore
                if not device_name:
                    capture.user_agent = capture_ua
                await capture.initialize_context()
                entries = await capture.capture_page(url, referer=referer)
        except PlaywrightCaptureException as e:
            self.logger.exception(f'Invalid parameters for the capture of {url} - {e}')
            return False, 'Invalid parameters for the capture of {url} - {e}'

        except Exception as e:
            self.logger.exception(f'Something went terribly wrong when capturing {url} - {e}')
            return False, f'Something went terribly wrong when capturing {url}.'

        if not entries:
            # broken
            self.logger.critical(f'Something went terribly wrong when capturing {url}.')
            return False, f'Something went terribly wrong when capturing {url}.'
        now = datetime.now()
        dirpath = self.capture_dir / str(now.year) / f'{now.month:02}' / now.isoformat()
        safe_create_dir(dirpath)

        if os or browser:
            meta = {}
            if os:
                meta['os'] = os
            if browser:
                meta['browser'] = browser
            with (dirpath / 'meta').open('w') as _meta:
                json.dump(meta, _meta)

        # Write UUID
        with (dirpath / 'uuid').open('w') as _uuid:
            _uuid.write(perma_uuid)

        # Write no_index marker (optional)
        if not listing:
            (dirpath / 'no_index').touch()

        # Write parent UUID (optional)
        if parent:
            with (dirpath / 'parent').open('w') as _parent:
                _parent.write(parent)

        if 'downloaded_filename' in entries and entries['downloaded_filename']:
            with (dirpath / '0.data.filename').open('w') as _downloaded_filename:
                _downloaded_filename.write(entries['downloaded_filename'])

        if 'downloaded_file' in entries and entries['downloaded_file']:
            with (dirpath / '0.data').open('wb') as _downloaded_file:
                _downloaded_file.write(entries['downloaded_file'])

        if 'error' in entries:
            with (dirpath / 'error.txt').open('w') as _error:
                json.dump(entries['error'], _error)

        if 'har' not in entries:
            return False, entries['error'] if entries['error'] else "Unknown error"

        with (dirpath / '0.har').open('w') as _har:
            json.dump(entries['har'], _har)

        if 'png' in entries and entries['png']:
            with (dirpath / '0.png').open('wb') as _img:
                _img.write(entries['png'])

        if 'html' in entries and entries['html']:
            with (dirpath / '0.html').open('w') as _html:
                _html.write(entries['html'])

        if 'last_redirected_url' in entries and entries['last_redirected_url']:
            with (dirpath / '0.last_redirect.txt').open('w') as _redir:
                _redir.write(entries['last_redirected_url'])

        if 'cookies' in entries and entries['cookies']:
            with (dirpath / '0.cookies.json').open('w') as _cookies:
                json.dump(entries['cookies'], _cookies)
        await self.redis.hset('lookup_dirs', perma_uuid, str(dirpath))
        return True, 'All good!'

    async def _to_run_forever_async(self):
        self.redis: Redis = Redis(unix_socket_path=get_socket_path('cache'))
        while await self.redis.exists('to_capture'):
            await self.process_capture_queue()
            if self.shutdown_requested():
                break
        await self.redis.close()


def main():
    m = AsyncCapture()
    asyncio.run(m.run_async(sleep_in_sec=1))


if __name__ == '__main__':
    main()
