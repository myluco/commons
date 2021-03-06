# ==================================================================================================
# Copyright 2011 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================

import errno
import hashlib
import os
import shutil

from abc import ABCMeta, abstractmethod
from collections import namedtuple

from twitter.common.lang import Compatibility
from twitter.common.dirutil import safe_rmtree


CacheKey = namedtuple('CacheKey', ['sources', 'hash', 'filename'])


class SourceScope(object):
  """Selects sources of a given scope from targets."""

  __metaclass__ = ABCMeta

  @staticmethod
  def for_selector(selector):
    class Scope(SourceScope):
      def select(self, target):
        return selector(target)
    return Scope()

  @abstractmethod
  def select(self, target):
    """Selects source files from the given target and returns them as absolute paths."""

  def valid(self, target):
    """Returns True if the given target can be used with this SourceScope."""
    return hasattr(target, 'expand_files')


NO_SOURCES = SourceScope.for_selector(lambda t: ())
TARGET_SOURCES = SourceScope.for_selector(
  lambda t: t.expand_files(recursive=False, include_buildfile=False)
)
TRANSITIVE_SOURCES = SourceScope.for_selector(
  lambda t: t.expand_files(recursive=True, include_buildfile=False)
)


class BuildCache(object):
  """Caches build artifacts and invalidates based on the SHA1 hash of source files."""

  VERSION = 0

  def __init__(self, root):
    self._root = os.path.join(root, str(BuildCache.VERSION))
    try:
      os.makedirs(self._root)
    except OSError as e:
      if e.errno != errno.EEXIST:
        raise

  def key_for(self, id, sources):
    """Get a cache key representing the given id'd entity and its associated source files."""
    return self._key_for(id, self._sources_hash(sources), sources)

  def key_for_target(self, target, sources=TARGET_SOURCES, fingerprint_extra=None):
    """Get a key representing the given target name and its sources.

    :target: The target to create a CacheKey for.
    :sources: A source scope to select from the target for hashing, defaults to TARGET_SOURCES.
    :fingerprint_extra: A that accepts a sha hash and updates it with extra fingerprint data.
    """
    if (not sources or not sources.valid(target)) and not fingerprint_extra:
      raise ValueError('A target needs to have at least one of sources or a '
                       'fingerprint_extra function to generate a CacheKey.')
    if not sources:
      sources = NO_SOURCES

    srcs = sorted(sources.select(target))
    sha = self._sources_hash(srcs)
    if fingerprint_extra:
      fingerprint_extra(sha)
    return self._key_for(target.id, sha, srcs)

  def _key_for(self, id, sha, sources):
    filename = os.path.join(self._root, id)
    return CacheKey(sources, sha.hexdigest(), filename)

  def invalidate(self, cache_key):
    """Invalidates this cache key and any cached files associated with it.

    :param cache_key: A CacheKey object (as returned by BuildCache.key_for().
    """
    safe_rmtree(cache_key.filename)
    sha_file = self._sha_file(cache_key)
    if os.path.exists(sha_file):
      os.unlink(sha_file)

  def needs_update(self, cache_key):
    """Check if the given target is cached.

    :param cache_key: A CacheKey object (as returned by BuildCache.key_for().
    :returns: True if the cached version of the target is out of date.
    """
    cached_sha = self._read_sha(cache_key)
    return cached_sha != cache_key.hash

  def update(self, cache_key, build_artifacts=None, artifact_root=None):
    """Cache the output of a build.

    If the cache area contains an existing object with the same (path, source_sha) its path will
    be returned. If no such object exists, builder will be called with a path inside the staging
    area and should create a new object.

    :param cache_key: A CacheKey object (typically returned by BuildCache.key_for().
    :param build_artifacts: List of paths to generated artifacts under artifact_root.
    :param artifact_root: Optional root directory under which artifacts are stored.
    """
    safe_rmtree(cache_key.filename)
    for artifact in build_artifacts or ():
      rel_path = os.path.basename(artifact) \
          if artifact_root is None \
          else os.path.relpath(artifact, artifact_root)
      assert not rel_path.startswith('..'), \
        'Weird: artifact=%s, rel_path=%s' % (artifact, rel_path)
      artifact_dest = os.path.join(cache_key.filename, rel_path)
      dir_name = os.path.dirname(artifact_dest)
      if not os.path.exists(dir_name):
        os.makedirs(dir_name)
      if os.path.isdir(artifact):
        shutil.copytree(artifact, artifact_dest)
      else:
        shutil.copy(artifact, artifact_dest)
    self._write_sha(cache_key)

  def use_cached_files(self, cache_key, copy_fn):
    """Use cached files, typically by hard-linking them from the cache into a staging area.

    :param cache_key: A CacheKey object (typically returned by BuildCache.key_for().
    :param copy_fn: A function with the signature copy_fn(absolute_src_path, relative_dst_path) that
        will copy cached files into the desired destination.
    """
    for relative_filename, filename in self._walk_paths([cache_key.filename]):
      copy_fn(filename, relative_filename)

  def _walk_paths(self, paths):
    """Recursively walk the given paths.

    :returns: Iterable of (relative_path, absolute_path).
    """
    assert not isinstance(paths, Compatibility.string)
    for path in sorted(paths):
      if os.path.isdir(path):
        for dir_name, _, filenames in sorted(os.walk(path)):
          for filename in filenames:
            filename = os.path.join(dir_name, filename)
            yield os.path.relpath(filename, path), filename
      else:
        yield os.path.basename(path), path

  def _sources_hash(self, paths):
    """Generate SHA1 digest from the content of all files under the given paths."""
    sha = hashlib.sha1()

    for relative_filename, filename in self._walk_paths(paths):
      with open(filename, "rb") as fd:
        sha.update(Compatibility.to_bytes(relative_filename))
        sha.update(fd.read())

    return sha

  def _key(self, key):
    return key.replace(os.path.sep, '.')

  def _sha_file(self, cache_key):
    return os.path.join(self._root, cache_key.filename) + '.hash'

  def _write_sha(self, cache_key):
    with open(self._sha_file(cache_key), 'w') as fd:
      fd.write(cache_key.hash)

  def _read_sha(self, cache_key):
    try:
      with open(self._sha_file(cache_key), 'rb') as fd:
        return fd.read().strip()
    except IOError as e:
      if e.errno != errno.ENOENT:
        raise
