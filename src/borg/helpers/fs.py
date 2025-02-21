import errno
import os
import os.path
import re
import stat
import subprocess
import sys
import textwrap

from .errors import Error

from .process import prepare_subprocess_env
from ..platformflags import is_win32

from ..constants import *  # NOQA

from ..logger import create_logger
logger = create_logger()


py_37_plus = sys.version_info >= (3, 7)


def ensure_dir(path, mode=stat.S_IRWXU, pretty_deadly=True):
    """
    Ensures that the dir exists with the right permissions.
    1) Make sure the directory exists in a race-free operation
    2) If mode is not None and the directory has been created, give the right
    permissions to the leaf directory
    3) If pretty_deadly is True, catch exceptions, reraise them with a pretty
    message.
    Returns if the directory has been created and has the right permissions,
    An exception otherwise. If a deadly exception happened it is reraised.
    """
    try:
        os.makedirs(path, mode=mode, exist_ok=True)
    except OSError as e:
        if pretty_deadly:
            raise Error(e.args[1])
        else:
            raise


def get_base_dir():
    """Get home directory / base directory for borg:

    - BORG_BASE_DIR, if set
    - HOME, if set
    - ~$USER, if USER is set
    - ~
    """
    base_dir = os.environ.get('BORG_BASE_DIR') or os.environ.get('HOME')
    # os.path.expanduser() behaves differently for '~' and '~someuser' as
    # parameters: when called with an explicit username, the possibly set
    # environment variable HOME is no longer respected. So we have to check if
    # it is set and only expand the user's home directory if HOME is unset.
    if not base_dir:
        base_dir = os.path.expanduser('~%s' % os.environ.get('USER', ''))
    return base_dir


def get_keys_dir():
    """Determine where to repository keys and cache"""
    keys_dir = os.environ.get('BORG_KEYS_DIR')
    if keys_dir is None:
        # note: do not just give this as default to the environment.get(), see issue #5979.
        keys_dir = os.path.join(get_config_dir(), 'keys')
    ensure_dir(keys_dir)
    return keys_dir


def get_security_dir(repository_id=None):
    """Determine where to store local security information."""
    security_dir = os.environ.get('BORG_SECURITY_DIR')
    if security_dir is None:
        # note: do not just give this as default to the environment.get(), see issue #5979.
        security_dir = os.path.join(get_config_dir(), 'security')
    if repository_id:
        security_dir = os.path.join(security_dir, repository_id)
    ensure_dir(security_dir)
    return security_dir


def get_cache_dir():
    """Determine where to repository keys and cache"""
    # Get cache home path
    cache_home = os.path.join(get_base_dir(), '.cache')
    # Try to use XDG_CACHE_HOME instead if BORG_BASE_DIR isn't explicitly set
    if not os.environ.get('BORG_BASE_DIR'):
        cache_home = os.environ.get('XDG_CACHE_HOME', cache_home)
    # Use BORG_CACHE_DIR if set, otherwise assemble final path from cache home path
    cache_dir = os.environ.get('BORG_CACHE_DIR', os.path.join(cache_home, 'borg'))
    # Create path if it doesn't exist yet
    ensure_dir(cache_dir)
    cache_tag_fn = os.path.join(cache_dir, CACHE_TAG_NAME)
    if not os.path.exists(cache_tag_fn):
        cache_tag_contents = CACHE_TAG_CONTENTS + textwrap.dedent("""
        # This file is a cache directory tag created by Borg.
        # For information about cache directory tags, see:
        #       http://www.bford.info/cachedir/spec.html
        """).encode('ascii')
        from ..platform import SaveFile
        try:
            with SaveFile(cache_tag_fn, binary=True) as fd:
                fd.write(cache_tag_contents)
        except FileExistsError:
            # if we have multiple SaveFile calls running in parallel for same cache_tag_fn,
            # it is fine if just one (usually first/quicker one) of them run gets through
            # and all others raise FileExistsError.
            pass
    return cache_dir


def get_config_dir():
    """Determine where to store whole config"""
    # Get config home path
    config_home = os.path.join(get_base_dir(), '.config')
    # Try to use XDG_CONFIG_HOME instead if BORG_BASE_DIR isn't explicitly set
    if not os.environ.get('BORG_BASE_DIR'):
        config_home = os.environ.get('XDG_CONFIG_HOME', config_home)
    # Use BORG_CONFIG_DIR if set, otherwise assemble final path from config home path
    config_dir = os.environ.get('BORG_CONFIG_DIR', os.path.join(config_home, 'borg'))
    # Create path if it doesn't exist yet
    ensure_dir(config_dir)
    return config_dir


def dir_is_cachedir(path):
    """Determines whether the specified path is a cache directory (and
    therefore should potentially be excluded from the backup) according to
    the CACHEDIR.TAG protocol
    (http://www.bford.info/cachedir/spec.html).
    """

    tag_path = os.path.join(path, CACHE_TAG_NAME)
    try:
        if os.path.exists(tag_path):
            with open(tag_path, 'rb') as tag_file:
                tag_data = tag_file.read(len(CACHE_TAG_CONTENTS))
                if tag_data == CACHE_TAG_CONTENTS:
                    return True
    except OSError:
        pass
    return False


def dir_is_tagged(path, exclude_caches, exclude_if_present):
    """Determines whether the specified path is excluded by being a cache
    directory or containing user-specified tag files/directories. Returns a
    list of the names of the tag files/directories (either CACHEDIR.TAG or the
    matching user-specified files/directories).
    """
    # TODO: do operations based on the directory fd
    tag_names = []
    if exclude_caches and dir_is_cachedir(path):
        tag_names.append(CACHE_TAG_NAME)
    if exclude_if_present is not None:
        for tag in exclude_if_present:
            tag_path = os.path.join(path, tag)
            if os.path.exists(tag_path):
                tag_names.append(tag)
    return tag_names


_safe_re = re.compile(r'^((\.\.)?/+)+')


def make_path_safe(path):
    """Make path safe by making it relative and local
    """
    return _safe_re.sub('', path) or '.'


def hardlinkable(mode):
    """return True if we support hardlinked items of this type"""
    return stat.S_ISREG(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode) or stat.S_ISFIFO(mode)


def scandir_keyfunc(dirent):
    try:
        return (0, dirent.inode())
    except OSError as e:
        # maybe a permission denied error while doing a stat() on the dirent
        logger.debug('scandir_inorder: Unable to stat %s: %s', dirent.path, e)
        # order this dirent after all the others lexically by file name
        # we may not break the whole scandir just because of an exception in one dirent
        # ignore the exception for now, since another stat will be done later anyways
        # (or the entry will be skipped by an exclude pattern)
        return (1, dirent.name)


def scandir_inorder(*, path, fd=None):
    # py37+ supports giving an fd instead of a path (no full entry.path in DirEntry in that case!)
    arg = fd if fd is not None and py_37_plus else path
    return sorted(os.scandir(arg), key=scandir_keyfunc)


def secure_erase(path):
    """Attempt to securely erase a file by writing random data over it before deleting it."""
    with open(path, 'r+b') as fd:
        length = os.stat(fd.fileno()).st_size
        fd.write(os.urandom(length))
        fd.flush()
        os.fsync(fd.fileno())
    os.unlink(path)


def truncate_and_unlink(path):
    """
    Truncate and then unlink *path*.

    Do not create *path* if it does not exist.
    Open *path* for truncation in r+b mode (=O_RDWR|O_BINARY).

    Use this when deleting potentially large files when recovering
    from a VFS error such as ENOSPC. It can help a full file system
    recover. Refer to the "File system interaction" section
    in repository.py for further explanations.
    """
    try:
        with open(path, 'r+b') as fd:
            fd.truncate()
    except OSError as err:
        if err.errno != errno.ENOTSUP:
            raise
        # don't crash if the above ops are not supported.
    os.unlink(path)


def dash_open(path, mode):
    assert '+' not in mode  # the streams are either r or w, but never both
    if path == '-':
        stream = sys.stdin if 'r' in mode else sys.stdout
        return stream.buffer if 'b' in mode else stream
    else:
        return open(path, mode)


def O_(*flags):
    result = 0
    for flag in flags:
        result |= getattr(os, 'O_' + flag, 0)
    return result


flags_base = O_('BINARY', 'NOCTTY', 'RDONLY')
flags_special = flags_base | O_('NOFOLLOW')  # BLOCK == wait when reading devices or fifos
flags_special_follow = flags_base  # BLOCK == wait when reading symlinked devices or fifos
flags_normal = flags_base | O_('NONBLOCK', 'NOFOLLOW')
flags_noatime = flags_normal | O_('NOATIME')
flags_root = O_('RDONLY')
flags_dir = O_('DIRECTORY', 'RDONLY', 'NOFOLLOW')


def os_open(*, flags, path=None, parent_fd=None, name=None, noatime=False):
    """
    Use os.open to open a fs item.

    If parent_fd and name are given, they are preferred and openat will be used,
    path is not used in this case.

    :param path: full (but not necessarily absolute) path
    :param parent_fd: open directory file descriptor
    :param name: name relative to parent_fd
    :param flags: open flags for os.open() (int)
    :param noatime: True if access time shall be preserved
    :return: file descriptor
    """
    if name and parent_fd is not None:
        # name is neither None nor empty, parent_fd given.
        fname = name  # use name relative to parent_fd
    else:
        fname, parent_fd = path, None  # just use the path
    if is_win32 and os.path.isdir(fname):
        # Directories can not be opened on Windows.
        return None
    _flags_normal = flags
    if noatime:
        _flags_noatime = _flags_normal | O_('NOATIME')
        try:
            # if we have O_NOATIME, this likely will succeed if we are root or owner of file:
            fd = os.open(fname, _flags_noatime, dir_fd=parent_fd)
        except PermissionError:
            if _flags_noatime == _flags_normal:
                # we do not have O_NOATIME, no need to try again:
                raise
            # Was this EPERM due to the O_NOATIME flag? Try again without it:
            fd = os.open(fname, _flags_normal, dir_fd=parent_fd)
        except OSError as exc:
            # O_NOATIME causes EROFS when accessing a volume shadow copy in WSL1
            from . import workarounds
            if 'retry_erofs' in workarounds and exc.errno == errno.EROFS and _flags_noatime != _flags_normal:
                fd = os.open(fname, _flags_normal, dir_fd=parent_fd)
            else:
                raise
    else:
        fd = os.open(fname, _flags_normal, dir_fd=parent_fd)
    return fd


def os_stat(*, path=None, parent_fd=None, name=None, follow_symlinks=False):
    """
    Use os.stat to open a fs item.

    If parent_fd and name are given, they are preferred and statat will be used,
    path is not used in this case.

    :param path: full (but not necessarily absolute) path
    :param parent_fd: open directory file descriptor
    :param name: name relative to parent_fd
    :return: stat info
    """
    if name and parent_fd is not None:
        # name is neither None nor empty, parent_fd given.
        fname = name  # use name relative to parent_fd
    else:
        fname, parent_fd = path, None  # just use the path
    return os.stat(fname, dir_fd=parent_fd, follow_symlinks=follow_symlinks)


def umount(mountpoint):
    env = prepare_subprocess_env(system=True)
    try:
        return subprocess.call(['fusermount', '-u', mountpoint], env=env)
    except FileNotFoundError:
        return subprocess.call(['umount', mountpoint], env=env)
