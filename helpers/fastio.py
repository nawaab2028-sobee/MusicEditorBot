"""
Fast transfer layer for Pyrofork.

Why: stock Pyrogram downloads a file chunk-by-chunk (1 request at a time) and uploads files
below 10 MB with a single worker (512 KB at a time). Every round trip is then paid in full, so
speed is limited by latency instead of bandwidth. Audio files are almost always < 10 MB, which is
why upload felt even slower than download.

What this does (nothing else in the bot needs to change):
  * get_file  : many 1 MB requests in flight at the same time, chunks are still yielded in order
  * save_file : many parts uploaded in parallel for EVERY file size, progress = really acknowledged bytes
  * foreign-DC auth keys are cached, so the expensive key exchange is not repeated for every file
  * a failed chunk/part now raises an error instead of silently producing a truncated/corrupt file

Anything unusual (chat photos, CDN redirect, resume of a missing part, unknown file size, partial
ranges) is handed over to the original Pyrofork implementation.
"""

import asyncio
import functools
import inspect
import io
import math
import os
from collections import deque
from hashlib import md5
from pathlib import PurePath

import pyrogram
from pyrogram import Client, raw
from pyrogram.errors import AuthKeyUnregistered, FloodWait
from pyrogram.file_id import FileId, FileType
from pyrogram.session import Auth, Session

from config import DOWNLOAD_PARALLEL, UPLOAD_PARALLEL
from logger import LOGGER

logger = LOGGER(__name__)

DL_CHUNK = 1024 * 1024      # download request size (Telegram max)
UL_PART = 512 * 1024        # upload part size (Telegram max)
BIG_FILE = 10 * 1024 * 1024
RETRIES = 3


async def _call_progress(client, progress, args):
    if not progress:
        return
    try:
        if inspect.iscoroutinefunction(progress):
            await progress(*args)
        else:
            await client.loop.run_in_executor(client.executor, functools.partial(progress, *args))
    except pyrogram.StopTransmission:
        raise
    except Exception as e:  # a broken progress callback must never break the transfer
        logger.debug(f"progress callback failed: {e}")


class FastClient(Client):
    """Drop-in replacement for pyrogram.Client with parallel get_file / save_file."""

    _dc_keys = None  # dc_id -> authorized auth key (media sessions on foreign DCs)

    # --------------------------------------------------
    # media session (with cached auth key for foreign DCs)
    # --------------------------------------------------
    async def _fast_session(self, dc_id):
        """Returns (session, used_cached_key)."""
        if self._dc_keys is None:
            self._dc_keys = {}
        test_mode = await self.storage.test_mode()
        home = await self.storage.dc_id()

        if dc_id == home:
            session = Session(self, dc_id, await self.storage.auth_key(), test_mode, is_media=True)
            await session.start()
            return session, False

        cached = self._dc_keys.get(dc_id)
        if cached:
            session = Session(self, dc_id, cached, test_mode, is_media=True)
            try:
                await session.start()
                return session, True
            except Exception as e:
                logger.info(f"cached DC{dc_id} key rejected ({e}); creating a new one")
                self._dc_keys.pop(dc_id, None)
                try:
                    await session.stop()
                except Exception:
                    pass

        auth_key = await Auth(self, dc_id, test_mode).create()
        session = Session(self, dc_id, auth_key, test_mode, is_media=True)
        await session.start()
        try:
            exported = await self.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc_id))
            await session.invoke(raw.functions.auth.ImportAuthorization(id=exported.id, bytes=exported.bytes))
        except Exception:
            try:
                await session.stop()
            except Exception:
                pass
            raise
        self._dc_keys[dc_id] = auth_key
        return session, False

    # --------------------------------------------------
    # download
    # --------------------------------------------------
    async def get_file(self, file_id: FileId, file_size: int = 0, limit: int = 0, offset: int = 0,
                       progress=None, progress_args=()):
        fast = (
            DOWNLOAD_PARALLEL > 1
            and file_id.file_type in (FileType.DOCUMENT, FileType.AUDIO, FileType.VOICE, FileType.VIDEO,
                                      FileType.PHOTO, FileType.ANIMATION, FileType.VIDEO_NOTE,
                                      FileType.STICKER)
            and file_size > DL_CHUNK
            and not limit and not offset
        )
        if not fast:
            async for chunk in super().get_file(file_id, file_size, limit, offset, progress, progress_args):
                yield chunk
            return

        async with self.get_file_semaphore:
            if file_id.file_type == FileType.PHOTO:
                location = raw.types.InputPhotoFileLocation(
                    id=file_id.media_id, access_hash=file_id.access_hash,
                    file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size)
            else:
                location = raw.types.InputDocumentFileLocation(
                    id=file_id.media_id, access_hash=file_id.access_hash,
                    file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size)

            session = None
            tasks = deque()
            try:
                session, used_cached = await self._fast_session(file_id.dc_id)

                async def fetch(idx):
                    last = None
                    for attempt in range(RETRIES):
                        try:
                            return await session.invoke(
                                raw.functions.upload.GetFile(location=location, offset=idx * DL_CHUNK, limit=DL_CHUNK),
                                sleep_threshold=30)
                        except (asyncio.TimeoutError, TimeoutError, OSError) as e:
                            last = e
                            await asyncio.sleep(0.5 * (attempt + 1))
                    raise last

                try:
                    first = await fetch(0)
                except AuthKeyUnregistered:
                    if not used_cached:
                        raise
                    self._dc_keys.pop(file_id.dc_id, None)
                    await session.stop()
                    session, _ = await self._fast_session(file_id.dc_id)
                    first = await fetch(0)

                if not isinstance(first, raw.types.upload.File):
                    # CDN redirect etc. -> original implementation from the start
                    await session.stop()
                    session = None
                    async for chunk in super().get_file(file_id, file_size, limit, offset, progress, progress_args):
                        yield chunk
                    return

                total_chunks = math.ceil(file_size / DL_CHUNK)
                yield first.bytes
                done = 1
                await _call_progress(self, progress, (min(done * DL_CHUNK, file_size), file_size, *progress_args))
                if len(first.bytes) < DL_CHUNK:
                    return

                nxt = 1
                while True:
                    while len(tasks) < DOWNLOAD_PARALLEL and nxt < total_chunks:
                        tasks.append(asyncio.ensure_future(fetch(nxt)))
                        nxt += 1
                    if not tasks:
                        break
                    r = await tasks.popleft()
                    if not isinstance(r, raw.types.upload.File):
                        raise RuntimeError("unexpected response while downloading")
                    chunk = r.bytes
                    if chunk:
                        yield chunk
                    done += 1
                    await _call_progress(self, progress, (min(done * DL_CHUNK, file_size), file_size, *progress_args))
                    if len(chunk) < DL_CHUNK:
                        break
            finally:
                for t in tasks:
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if session is not None:
                    try:
                        await session.stop()
                    except Exception:
                        pass

    # --------------------------------------------------
    # upload
    # --------------------------------------------------
    async def save_file(self, path, file_id=None, file_part=0, progress=None, progress_args=()):
        if path is None:
            return None
        if file_id is not None or UPLOAD_PARALLEL <= 1:  # resume of a missing part -> original
            return await super().save_file(path, file_id, file_part, progress, progress_args)

        async with self.save_file_semaphore:
            if isinstance(path, (str, PurePath)):
                fp = open(path, "rb")
                close_fp = True
            elif isinstance(path, io.IOBase):
                fp = path
                close_fp = False
            else:
                raise ValueError("Invalid file. Expected a file path as string or a binary (not text) file pointer")

            session = None
            workers = []
            ticker = None
            try:
                file_name = getattr(fp, "name", "file.jpg")
                fp.seek(0, io.SEEK_END)
                file_size = fp.tell()
                fp.seek(0)

                if file_size == 0:
                    raise ValueError("File size equals to 0 B")
                limit_mib = 4000 if self.me.is_premium else 2000
                if file_size > limit_mib * 1024 * 1024:
                    raise ValueError(f"Can't upload files bigger than {limit_mib} MiB")

                total_parts = int(math.ceil(file_size / UL_PART))
                is_big = file_size > BIG_FILE
                md5_sum = None if is_big else md5()
                up_id = self.rnd_id()
                n_workers = max(1, min(UPLOAD_PARALLEL, total_parts))
                queue = asyncio.Queue(n_workers * 2)
                state = {"bytes": 0, "error": None}

                session = Session(self, await self.storage.dc_id(), await self.storage.auth_key(),
                                  await self.storage.test_mode(), is_media=True)
                await session.start()

                async def worker():
                    while True:
                        item = await queue.get()
                        if item is None:
                            return
                        if state["error"] is not None:
                            continue  # drain
                        rpc, size = item
                        ok, last = False, None
                        for attempt in range(RETRIES):
                            try:
                                await session.invoke(rpc)
                                ok = True
                                break
                            except FloodWait as e:
                                last = e
                                await asyncio.sleep(min(e.value, 30) + 1)
                            except (asyncio.TimeoutError, TimeoutError, OSError) as e:
                                last = e
                                await asyncio.sleep(0.5 * (attempt + 1))
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                last = e
                                break
                        if ok:
                            state["bytes"] += size
                        else:
                            state["error"] = last

                workers = [asyncio.create_task(worker()) for _ in range(n_workers)]

                async def tick():
                    while True:
                        await asyncio.sleep(1.0)
                        await _call_progress(self, progress, (min(state["bytes"], file_size), file_size, *progress_args))

                if progress:
                    ticker = asyncio.create_task(tick())

                part = 0
                while True:
                    chunk = await asyncio.to_thread(fp.read, UL_PART) if file_size > 64 * UL_PART else fp.read(UL_PART)
                    if not chunk:
                        break
                    if is_big:
                        rpc = raw.functions.upload.SaveBigFilePart(
                            file_id=up_id, file_part=part, file_total_parts=total_parts, bytes=chunk)
                    else:
                        rpc = raw.functions.upload.SaveFilePart(file_id=up_id, file_part=part, bytes=chunk)
                        md5_sum.update(chunk)
                    await queue.put((rpc, len(chunk)))
                    part += 1
                    if state["error"] is not None:
                        break

                for _ in workers:
                    await queue.put(None)
                await asyncio.gather(*workers)
                workers = []

                if state["error"] is not None:
                    raise state["error"]
                await _call_progress(self, progress, (file_size, file_size, *progress_args))

                if is_big:
                    return raw.types.InputFileBig(id=up_id, parts=total_parts, name=file_name)
                digest = "".join(hex(i)[2:].zfill(2) for i in md5_sum.digest())
                return raw.types.InputFile(id=up_id, parts=total_parts, name=file_name, md5_checksum=digest)
            finally:
                if ticker:
                    ticker.cancel()
                for w in workers:
                    w.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
                if session is not None:
                    try:
                        await session.stop()
                    except Exception:
                        pass
                if close_fp:
                    fp.close()
