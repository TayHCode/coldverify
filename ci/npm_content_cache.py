import contextlib
import hashlib
import os
import re
import stat
import sys
from pathlib import Path


def directory_flags():
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextlib.contextmanager
def content_store(home, create=False):
    if sys.platform != 'linux' or not home or not Path(home).is_absolute():
        yield None
        return
    descriptor = None
    try:
        path = Path(os.path.abspath(home)) / '.npm' / '_cacache' / 'content-v2'
        descriptor = os.open('/', directory_flags())
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as error:
        if not isinstance(error, FileNotFoundError):
            print(f'npm download cache unavailable: {error.strerror}', file=sys.stderr)
        if descriptor is not None:
            os.close(descriptor)
        descriptor = None
    try:
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextlib.contextmanager
def subdirectory(parent, name, create=False):
    descriptor = None
    try:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        descriptor = os.open(name, directory_flags(), dir_fd=parent)
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def copy_content(source, destination, exclude=()):
    copied = set()
    rejected = 0
    if source is None or destination is None:
        return copied

    def walk(src, dst, parts):
        nonlocal rejected
        for name in os.listdir(src):
            width = (None, 2, 2, 124)[len(parts)]
            if not (name == 'sha512' if width is None else re.fullmatch('[0-9a-f]{%d}' % width, name)):
                continue
            relative = (*parts, name)
            try:
                if len(parts) < 3:
                    with subdirectory(src, name) as child_src, subdirectory(dst, name, True) as child_dst:
                        walk(child_src, child_dst, relative)
                elif relative not in exclude:
                    if insert(src, dst, name, ''.join(relative[1:])):
                        copied.add(relative)
                    else:
                        rejected += 1
            except OSError:
                rejected += 1

    def insert(src, dst, name, digest):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src)
        with os.fdopen(descriptor, 'rb') as incoming:
            metadata = os.fstat(incoming.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                return
            output = os.open('.', os.O_TMPFILE | os.O_RDWR, 0o600, dir_fd=dst)
            with os.fdopen(output, 'w+b') as staged:
                checksum = hashlib.sha512()
                while block := incoming.read(1024 * 1024):
                    checksum.update(block)
                    staged.write(block)
                staged.flush()
                if checksum.hexdigest() != digest:
                    return False
                try:
                    os.link(f'/proc/self/fd/{staged.fileno()}', name, dst_dir_fd=dst,
                            follow_symlinks=True)
                except FileExistsError:
                    return True
                return True

    try:
        walk(source, destination, ())
    except OSError:
        rejected += 1
    if rejected:
        print(f'npm download cache: ignored {rejected} unsafe or unavailable objects', file=sys.stderr)
    return copied
