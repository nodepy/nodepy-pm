# Copyright (c) 2017 Niklas Rosenstein
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import print_function

import contextlib
import errno
import nodepy
import os
import pip.commands
import shlex
import shutil
import six
import subprocess
import sys
import tarfile
import tempfile
import traceback

from fnmatch import fnmatch

import _registry from './registry'
import _config from './config'
import _download from './util/download'
import _script from './util/script'
import refstring from './refstring'
import pathutils from './util/pathutils'
import brewfix from './brewfix'
import PackageLifecycle from './package-lifecycle'
import { PACKAGE_MANIFEST, get_directories, get_module_dist_info } from './env'
import { parse as parse_manifest } from './manifest'
import { PackageManifest, InvalidPackageManifest } from './manifest'

PACKAGE_LINK = '.nodepy-link'
NPPM_INSTALLED_FILES = 'installed-files.txt'


default_exclude_patterns = [
    '.DS_Store', '.svn/*', '.git*', 'nodepy_modules/*',
    '*.pyc', '*.pyo', 'dist/*']


def _makedirs(path):
  if not os.path.isdir(path):
    os.makedirs(path)


def _match_any_pattern(filename, patterns):
  return any(fnmatch(filename, x) for x in patterns)


def _check_include_file(filename, include_patterns, exclude_patterns):
  if _match_any_pattern(filename, exclude_patterns):
    return False
  if not include_patterns:
    return True
  return _match_any_pattern(filename, include_patterns)


class PackageNotFound(Exception):
  pass


def walk_package_files(manifest):
  """
  Walks over the files included in a package and yields (abspath, relpath).
  """

  inpat = manifest.dist.get('include_files', [])
  expat = manifest.dist.get('exclude_files', []) + default_exclude_patterns

  for root, __, files in os.walk(manifest.directory):
    for filename in files:
      filename = os.path.join(root, filename)
      rel = os.path.relpath(filename, manifest.directory)
      if rel == PACKAGE_MANIFEST or _check_include_file(rel, inpat, expat):
        yield (filename, rel)


class Installer:
  """
  This class manages the installation/uninstallation procedure.
  """

  def __init__(self, registry=None, upgrade=False, install_location='local',
      pip_separate_process=False, pip_use_target_option=False, recursive=False,
      verbose=False):
    assert install_location in ('local', 'global', 'root')
    self.reg = [registry] if registry else _registry.RegistryClient.get_all()
    self.upgrade = upgrade
    self.install_location = install_location
    self.pip_separate_process = pip_separate_process
    self.pip_use_target_option = pip_use_target_option
    self.recursive = recursive
    self.verbose = verbose
    self.dirs = get_directories(install_location)
    self.dirs['reference_dir'] = os.path.dirname(self.dirs['packages'])
    self.script = _script.ScriptMaker(self.dirs['bin'], self.install_location)
    self.ignore_installed = False
    self.force = False
    if install_location in ('local', 'global'):
      self.script.path.append(self.dirs['pip_bin'])
      self.script.pythonpath.extend([self.dirs['pip_lib']])
    self.installed_python_libs = {}

  @contextlib.contextmanager
  def pythonpath_update_context(self):
    self._old_sys_path = sys.path[:]
    self._old_pythonpath = os.getenv('PYTHONPATH', '')
    # Restore the previous path.
    if nodepy.script:
      sys.path[:] = nodepy.script['original_path']
    # Add the path to the local Pip library path to sys.path and the PYTHONPATH
    # environment variable to ensure that the current installation process can
    # also find the already installed packages (some setup scripts might import
    # third party modules). Fix for nodepy/ppym#10.
    if self.install_location != 'root':
      sys.path[:] = [self.dirs['pip_lib']] + sys.path
      os.environ['PYTHONPATH'] = os.path.abspath(self.dirs['pip_lib']) \
          + os.pathsep + self._old_pythonpath
    nodepy.reload_pkg_resources('pkg_resources')
    nodepy.reload_pkg_resources('pip._vendor.pkg_resources')
    try:
      yield
    finally:
      sys.path[:] = self._old_sys_path
      os.environ['PYTHONPATH'] = self._old_pythonpath
      nodepy.reload_pkg_resources('pkg_resources')
      nodepy.reload_pkg_resources('pip._vendor.pkg_resources')

  def find_package(self, package):
    """
    Finds an installed package and returns its #PackageManifest.
    Raises #PackageNotFound if the package could not be found, or possibly
    an #InvalidPackageManifest exception if the manifest is invalid.

    If #Installer.strict is set, the package is only looked for in the target
    packages directory instead of all possibly inherited paths.
    """

    refstring.parse_package(package)
    dirname = os.path.join(self.dirs['packages'], package)
    if not os.path.isdir(dirname):
      raise PackageNotFound(package)

    lnk = nodepy.get_package_link(dirname)
    if lnk:
      rel = os.path.relpath(dirname, lnk.src)
      assert rel == os.curdir, rel
      manifest_fn = os.path.join(lnk.dst, PACKAGE_MANIFEST)
    else:
      manifest_fn = os.path.join(dirname, PACKAGE_MANIFEST)

    if not os.path.isfile(manifest_fn):
      print('Warning: found package directory without {}'.format(PACKAGE_MANIFEST))
      print("  at '{}'".format(dirname))
      raise PackageNotFound(package)
    else:
      try:
        return parse_manifest(manifest_fn, directory=dirname)
      except InvalidPackageManifest as exc:
        print('Warning: invalid package manifest')
        print("  at '{}'".format(manifest_fn))
        return None

  def uninstall(self, package_name):
    """
    Uninstalls a package by name.
    """

    try:
      manifest = self.find_package(package_name)
    except PackageNotFound:
      print('Package "{}" not installed'.format(package_name))
      return False
    else:
      return self.uninstall_directory(manifest.directory)

  def uninstall_directory(self, directory):
    """
    Uninstalls a package from a directory. Returns True on success, False
    on failure.
    """

    link_fn = os.path.join(directory, PACKAGE_LINK)
    if os.path.isfile(link_fn):
      with open(link_fn, 'r') as fp:
        manifest_fn = os.path.join(fp.read().rstrip('\n'), PACKAGE_MANIFEST)
    else:
      manifest_fn = os.path.join(directory, PACKAGE_MANIFEST)

    try:
      manifest = parse_manifest(manifest_fn)
    except (OSError, IOError) as exc:
      if exc.errno != errno.ENOENT:
        raise
      if not self.force:
        print('Can not uninstall: directory "{}": No package manifest, please '
          'remove the directory manually or pass -f,--force'.format(directory))
        return False
      print('Removing previous directory: "{}"'.format(directory))
      shutil.rmtree(directory)
      return True
    except InvalidPackageManifest as exc:
      print('Can not uninstall: directory "{}": Invalid manifest": {}'.format(directory, exc))
      return False

    print('Uninstalling "{}" from "{}"{}...'.format(manifest.identifier,
        directory, ' before upgrade' if self.upgrade else ''))

    plc = PackageLifecycle(manifest=manifest)
    try:
      plc.run('pre-uninstall', [], script_only=True)
    except:
      traceback.print_exc()
      print('Error: pre-uninstall script failed.')
      return False

    filelist_fn = os.path.join(directory, NPPM_INSTALLED_FILES)
    installed_files = []
    if not os.path.isfile(filelist_fn):
      print('  Warning: No `{}` found in package directory'.format(NPPM_INSTALLED_FILES))
    else:
      with open(filelist_fn, 'r') as fp:
        for line in fp:
          installed_files.append(line.rstrip('\n'))

    for fn in installed_files:
      try:
        os.remove(fn)
        print('  Removed "{}"...'.format(fn))
      except OSError as exc:
        print('  "{}":'.format(fn), exc)
    _rmtree(directory)
    return True

  def install_dependencies_for(self, manifest, dev=False):
    """
    Installs the Node.py and Python dependencies of a #PackageManifest.
    """

    deps = dict(manifest.dependencies)
    if dev:
      deps.update(manifest.dev_dependencies)
    if deps:
      print('Installing dependencies for "{}"{}...'.format(manifest.identifier,
          ' (dev) ' if dev else ''))
      if not self.install_dependencies(deps, manifest.directory):
        return False

    deps = dict(manifest.python_dependencies)
    if dev:
      deps.update(manifest.dev_python_dependencies)
    if deps:
      print('Installing Python dependencies for "{}"{}...'.format(
          manifest.identifier, ' (dev) ' if dev else ''))
      if not self.install_python_dependencies(deps):
        return False

    return True

  def install_dependencies(self, deps, current_dir):
    """
    Install all dependencies specified in the dictionary *deps*.
    """

    install_deps = []
    for name, version in deps.items():
      try:
        dep = self.find_package(name)
      except PackageNotFound as exc:
        install_deps.append((name, version))
      else:
        if version.type == 'version':
          if not version.sel(dep.version):
            print('  Warning: Dependency "{}@{}" unsatisfied, have "{}" installed'
                .format(name, version, dep.identifier))
          else:
            print('  Skipping satisfied dependency "{}@{}", have "{}" installed'
                .format(name, version, dep.identifier))
        else:
          # Must be a Git URL or a relative path.
          print('  Skipping dependency "{}" from URL "{}", have "{}" '
              'installed'.format(name, version, dep.identifier))
          if self.recursive:
            self.install_dependencies_for(dep)

    if not install_deps:
      return True

    depfmt = ', '.join(refstring.join(n, version=v) for (n, v) in install_deps)
    print('  Installing dependencies:', depfmt)
    for name, version in install_deps:
      if version.type == 'git':
        if not self.install_from_git(version.url)[0]:
          return False
      elif version.type == 'path':
        path = version.path
        if not os.path.isabs(path):
          path = os.path.join(current_dir, path)
        if not self.install_from_directory(path, version.develop)[0]:
          return False
      elif version.type == 'version':
        if not self.install_from_registry(name, version.sel)[0]:
          return False
      else:
        raise RuntimeError('unsupported PackageVersion: {!r}'.format(version))

    return True

  def install_python_dependencies(self, deps, args=()):
    """
    Install all Python dependencies specified in *deps* using Pip. Make sure
    to call #relink_pip_scripts().
    """

    install_modules = []
    for name, version in deps.items():
      install_modules.append(name + version)

    if not install_modules and not args:
      return True

    # TODO: Upgrade strategy?

    if self.install_location in ('local', 'global'):
      if self.pip_use_target_option:
        cmd = ['--target', self.dirs['pip_lib']]
      else:
        cmd = ['--prefix', self.dirs['pip_prefix']]
    elif self.install_location == 'root':
      cmd = []
    else:
      raise RuntimeError('unexpected install location: {!r}'.format(self.install_location))

    cmd.extend(args)
    cmd.extend(install_modules)
    if self.ignore_installed:
      cmd += ['--ignore-installed']
    if self.upgrade:
      cmd += ['--upgrade']
    if self.verbose:
      cmd.append('--verbose')

    print('  Installing Python dependencies via Pip:', ' '.join(cmd),
        '(as a separate process)' if self.pip_separate_process else '')
    with brewfix(), self.pythonpath_update_context():
      if self.pip_separate_process:
        res = subprocess.call([sys.executable, '-m', 'pip', 'install'] + cmd)
      else:
        res = pip.commands.install.InstallCommand().main(cmd)
      if res != 0:
        print('Error: `pip install` failed with exit-code', res)
        return False

      # Important to use this function from within the updated pythonpath context.
      for dep_name in deps:
        self.installed_python_libs[dep_name] = get_module_dist_info(dep_name)

    return True

  def relink_pip_scripts(self):
    """
    Re-link scripts from the Pip bin directory to the Node.py bin directory.
    These scripts will extend the PYTHONPATH before they are executed to make
    sure that the respective modules can be found.
    """

    if self.install_location not in ('local',):
      return

    if os.name == 'nt':
      pathext = os.environ['PATHEXT'].lower().split(';')
    if os.path.isdir(self.dirs['pip_bin']):
      print('Relinking Pip-installed proxy scripts ...')
      for fn in os.listdir(self.dirs['pip_bin']):
        prefix = []
        if os.name == 'nt':
          script_name, ext = os.path.splitext(fn)
          ext = ext.lower()
          if not ext or ext not in pathext: continue
          if ext != '.exe':
            # If there is the same program as .exe, skip this one.
            if os.path.isfile(os.path.join(self.dirs['pip_bin'], script_name + '.exe')):
              continue
            prefix = ['cmd', '/C']
        else:
          script_name = fn

        target_prog = os.path.abspath(os.path.join(self.dirs['pip_bin'], fn))
        print('  Creating', script_name, 'from', target_prog, '...')
        self.script.make_wrapper(script_name, prefix + [target_prog])

  def install_from_directory(self, directory, develop=False, dev=False,
      expect=None, movedir=False):
    """
    Installs a package from a directory. The directory must have a
    `nodepy-package.toml` file. If *expect* is specified, it must be a tuple of
    (package_name, version) that is expected to be installed with *directory*.
    The argument is used by #install_from_registry().

    Returns True on success, False on failure.

    # Parameters
    directory (str): The directory to install from.
    develop (bool): True to install only a link to the package directory.
    dev (bool): True to install development dependencies.
    expect (None, (str, semver.Version)): If specified, a tuple of the
      name and version of the package that we expect to install from this
      directory.
    movedir (bool): This is set by #install_from_git() to move the source
      directory to the target install directory isntead of a normal
      install.

    # Returns
    (success, manifest)
    """

    filename = os.path.normpath(os.path.abspath(os.path.join(directory, PACKAGE_MANIFEST)))

    try:
      manifest = parse_manifest(filename)
    except (IOError, OSError) as exc:
      if exc.errno == errno.ENOENT:
        print('Error: directory "{}" contains no package manifest'.format(directory))
        return False, None
      raise
    except InvalidPackageManifest as exc:
      print('Error: directory "{}":'.format(directory), exc)
      return False, None

    if expect is not None and (manifest.name, manifest.version) != expect:
      print('Error: Expected to install "{}@{}" but got "{}" in "{}"'
          .format(expect[0], expect[1], manifest.identifier, directory))
      return False, manifest

    print('Installing "{}"...'.format(manifest.identifier))
    target_dir = os.path.join(self.dirs['packages'], manifest.name)

    # Error if the target directory already exists. The package must be
    # uninstalled before it can be installed again.
    if os.path.exists(target_dir):
      if not self.upgrade:
        print('  Note: install directory "{}" already exists, specify --upgrade'.format(target_dir))
        return True, manifest
      if not self.uninstall_directory(target_dir):
        return False, manifest

    plc = PackageLifecycle(manifest=manifest)
    try:
      plc.run('pre-install', [], script_only=True)
    except:
      traceback.print_exc()
      print('Error: pre-install script failed.')
      return False, manifest

    # Install dependencies.
    if not self.install_dependencies_for(manifest, dev=dev):
      return False, manifest

    installed_files = []

    if movedir:
      print('Moving "{}" to "{}" ...'.format(manifest.identifier, target_dir))
      print(os.path.exists(directory))
      _makedirs(os.path.dirname(target_dir))
      os.rename(directory, target_dir)
      installed_files.append(target_dir)
    else:
      print('Installing "{}" to "{}" ...'.format(manifest.identifier, target_dir))
      _makedirs(target_dir)
      if develop:
        # Create a link file that contains the path to the actual package directory.
        print('  Creating {} to "{}"...'.format(PACKAGE_LINK, directory))
        linkfn = os.path.join(target_dir, PACKAGE_LINK)
        with open(linkfn, 'w') as fp:
          fp.write(os.path.abspath(directory))
        installed_files.append(linkfn)
      else:
        for src, rel in walk_package_files(manifest):
          dst = os.path.join(target_dir, rel)
          _makedirs(os.path.dirname(dst))
          print('  Copying', rel, '...')
          shutil.copyfile(src, dst)
          installed_files.append(dst)

    # Create scripts for the 'bin' field in the package manifest.
    for script_name, filename in manifest.bin.items():
      if '${py}' in script_name:
        script_names = [
            script_name.replace('${py}', ''),
            script_name.replace('${py}', sys.version[0]),
            script_name.replace('${py}', sys.version[:3])
        ]
      else:
        script_names = [script_name]

      for script_name in script_names:
        print('  Installing script "{}" to "{}"...'.format(script_name, self.script.directory))
        if manifest.resolve_root:
          filename = os.path.join(manifest.resolve_root, filename)
        filename = os.path.abspath(os.path.join(target_dir, filename))
        installed_files += self.script.make_nodepy(
            script_name, filename, self.dirs['reference_dir'])

    # Write down the names of the installed files.
    with open(os.path.join(target_dir, NPPM_INSTALLED_FILES), 'w') as fp:
      for fn in installed_files:
        fp.write(fn)
        fp.write('\n')

    try:
      plc.run('post-install', [], script_only=True)
    except:
      traceback.print_exc()
      print('Error: post-install script failed.')
      return False, manifest

    return True, manifest

  def install_from_archive(self, archive, dev=False, expect=None):
    """
    Install a package from an archive.
    """

    directory = tempfile.mkdtemp(suffix='_' + os.path.basename(archive) + '_unpacked')
    print('Unpacking "{}"...'.format(archive))
    try:
      with tarfile.open(archive) as tar:
        tar.extractall(directory)
      return self.install_from_directory(directory, dev=dev, expect=expect)
    finally:
      _rmtree(directory)

  def install_from_registry(self, package_name, selector, dev=False):
    """
    Install a package from a registry.

    # Returns
    (success, (package_name, package_version))
    """

    # Check if the package already exists.
    try:
      package = self.find_package(package_name)
    except PackageNotFound:
      pass
    else:
      if not selector(package.version):
        print('  Warning: Dependency "{}@{}" unsatisfied, have "{}" installed'
            .format(package_name, selector, package.identifier))
      if not self.upgrade:
        print('package "{}@{}" already installed, specify --upgrade'.format(
            package.name, package.version))
        return True, (package.name, package.version)

    print('Finding package matching "{}@{}"...'.format(package_name, selector))
    for registry in self.reg:
      print('  Checking registry "{}" ({})...'.format(registry.name, registry.base_url), end=' ')
      try:
        info = registry.find_package(package_name, selector)
      except _registry.PackageNotFound as exc:
        print('NOT FOUND')
        continue
      else:
        print('FOUND ({}@{})'.format(info.name, info.version))
        break
    else:
      print('Error: package "{}@{}" could not be located'.format(package_name, selector))
      return False, None
    assert info.name == package_name, info

    print('Downloading "{}@{}"...'.format(info.name, info.version))
    response = registry.download(info.name, info.version)
    filename = _download.get_response_filename(response)

    tmp = None
    try:
      with tempfile.NamedTemporaryFile(suffix='_' + filename, delete=False) as tmp:
        progress = _download.DownloadProgress(30, prefix='  ')
        _download.download_to_fileobj(response, tmp, progress=progress)
      success = self.install_from_archive(tmp.name, dev=dev, expect=(package_name, info.version))
    finally:
      if tmp and os.path.isfile(tmp.name):
        os.remove(tmp.name)

    return success, (package_name, info.version)

  def install_from_git(self, url):
    """
    Install a package from a Git repository. The package will first be cloned
    into a temporary directory, that be copied into the correct location and
    binaries will be installed.

    # Returns
    (success, (package_name, package_version))
    """

    dest = os.path.join(self.dirs['packages'], '.tmp')
    res = subprocess.call(['git', 'clone', url, dest])
    if res != 0:
      print('Error: Git clone failed')
      return False, None

    with later(_rmtree, dest):
      success, manifest = self.install_from_directory(dest, movedir=True)

    if manifest:
      return success, (manifest.name, manifest.version)
    return success, None


class InstallError(Exception):
  pass


@contextlib.contextmanager
def later(__func, *args, **kwargs):
  try:
    yield
  finally:
    __func(*args, **kwargs)


def _rmtree(directory, ignore_errors=False):
  """
  This version of #shutil.rmtree() will set the appropriate permissions when
  an access error occurred.
  """

  def onerror(func, path, excinfo):
    if isinstance(excinfo[1], OSError) and excinfo[1].errno == errno.EACCES:
      os.chmod(path, int('777', 8))
      return func(path)
  shutil.rmtree(directory, ignore_errors=ignore_errors, onerror=onerror)
