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
from fnmatch import fnmatch
from nodepy.utils import pathlib

import contextlib
import errno
import nodepy.main
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

import _registry from './registry'
import _config from './config'
import _download from './util/download'
import _script from './util/script'
import refstring from './refstring'
import decorators from './util/decorators'
import brewfix from './brewfix'
import PackageLifecycle from './package-lifecycle'
import env, { PACKAGE_MANIFEST } from './env'
import manifest from './manifest'

default_exclude_patterns = [
    '.DS_Store', '.svn/*', '.git*', env.MODULES_DIRECTORY + '/*',
    '*.pyc', '*.pyo', 'dist/*']


def _makedirs(path):
  if not os.path.isdir(path):
    os.makedirs(path)


def _match_any_pattern(filename, patterns, gitignore_style=False):
  if not patterns:
    return False
  if os.name == 'nt':
    filename = filename.replace('\\', '/')
  for pattern in patterns:
    if filename == pattern or filename.startswith(pattern + '/') or \
        (gitignore_style and filename.endswith('/' + pattern)):
      return True
    if fnmatch(filename, pattern):
      return True
  return False


def _check_include_file(filename, include, exclude):
  if include is not None:
    return _match_any_pattern(filename, include)
  return not _match_any_pattern(filename, exclude)


class PackageNotFound(Exception):
  pass


class InvalidPackageManifest(Exception):
  pass


def walk_package_files(manifest):
  """
  Walks over the files included in a package and yields (abspath, relpath).
  """

  include = manifest.get('include', None)
  if include is None:
    exclude = manifest.get('exclude', []) + default_exclude_patterns
    ignore_file = os.path.join(manifest.directory, '.gitignore')
    if os.path.isfile(ignore_file):
      with open(ignore_file) as fp:
        for line in fp:
          line = line.strip()
          if not line: continue
          if line.startswith('#') or line.startswith('!'): continue
          exclude.append(line)
  else:
    exclude = None

  for root, __, files in os.walk(manifest.directory):
    for filename in files:
      filename = os.path.join(root, filename)
      rel = os.path.relpath(filename, manifest.directory)
      if rel == PACKAGE_MANIFEST or _check_include_file(rel, include, exclude):
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
    self.dirs = env.get_directories(install_location)
    self.dirs['reference_dir'] = os.path.dirname(self.dirs['packages'])
    self.script = _script.ScriptMaker(self.dirs['bin'], self.install_location)
    self.ignore_installed = False
    self.force = False
    if install_location in ('local', 'global'):
      self.script.path.append(self.dirs['pip_bin'])
      self.script.pythonpath.extend([self.dirs['pip_lib']])
    self.installed_python_libs = {}
    self.currently_installing = []  # stack of currently installing packages

  @contextlib.contextmanager
  def pythonpath_update_context(self):
    self._old_sys_path = sys.path[:]
    self._old_pythonpath = os.getenv('PYTHONPATH', '')
    # Restore the previous path.
    if nodepy.runtime.script:
      sys.path[:] = nodepy.runtime.script['original_path']
    # Add the path to the local Pip library path to sys.path and the PYTHONPATH
    # environment variable to ensure that the current installation process can
    # also find the already installed packages (some setup scripts might import
    # third party modules). Fix for nodepy/ppym#10.
    if self.install_location != 'root':
      sys.path[:] = [self.dirs['pip_lib']] + sys.path
      os.environ['PYTHONPATH'] = os.path.abspath(self.dirs['pip_lib']) \
          + os.pathsep + self._old_pythonpath
    nodepy.utils.machinery.reload_pkg_resources('pkg_resources')
    nodepy.utils.machinery.reload_pkg_resources('pip._vendor.pkg_resources')
    try:
      yield
    finally:
      sys.path[:] = self._old_sys_path
      os.environ['PYTHONPATH'] = self._old_pythonpath
      nodepy.utils.machinery.reload_pkg_resources('pkg_resources')
      nodepy.utils.machinery.reload_pkg_resources('pip._vendor.pkg_resources')

  def _load_manifest(self, filename, directory=None, do_raise=True):
    if not directory:
      directory = os.path.dirname(filename)
    mf = manifest.load(filename, directory=directory)
    if any(f.errors for f in manifest.validate(mf)):
      if do_raise:
        raise InvalidPackageManifest("invalid package manifest: {!r}".format(filename))
      print('Warning: invalid package manifest')
      print("  at '{}'".format(filename))
      return None
    return mf

  def find_package(self, package, internal=False):
    """
    Finds an installed package and returns its #PackageManifest.
    Raises #PackageNotFound if the package could not be found, or possibly
    an #InvalidPackageManifest exception if the manifest is invalid.

    If #Installer.strict is set, the package is only looked for in the target
    packages directory instead of all possibly inherited paths.
    """

    refstring.parse_package(package)
    if internal and self.currently_installing:
      dirname = os.path.join(self.currently_installing[-1][1], package)
    else:
      dirname = os.path.join(self.dirs['packages'], package)
    if not os.path.isdir(dirname):
      raise PackageNotFound(package)

    lnk = nodepy.resolver.StdResolver.resolve_link(require.context, pathlib.Path(dirname))
    if lnk:
      manifest_fn = os.path.join(lnk, PACKAGE_MANIFEST)
    else:
      manifest_fn = os.path.join(dirname, PACKAGE_MANIFEST)

    if not os.path.isfile(manifest_fn):
      print('Warning: found package directory without {}'.format(PACKAGE_MANIFEST))
      print("  at '{}'".format(dirname))
      raise PackageNotFound(package)
    else:
      return self._load_manifest(manifest_fn, directory=dirname)

  def uninstall(self, package_name, internal=False):
    """
    Uninstalls a package by name.
    """

    try:
      manifest = self.find_package(package_name, internal)
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

    link_fn = os.path.join(directory, env.LINK_FILE)
    if os.path.isfile(link_fn):
      with open(link_fn, 'r') as fp:
        manifest_fn = os.path.join(fp.read().rstrip('\n'), PACKAGE_MANIFEST)
    else:
      manifest_fn = os.path.join(directory, PACKAGE_MANIFEST)

    try:
      mf = self._load_manifest(manifest_fn)
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

    print('Uninstalling "{}" from "{}"{}...'.format(mf.identifier,
        directory, ' before upgrade' if self.upgrade else ''))

    plc = PackageLifecycle(manifest=mf)
    try:
      plc.run('pre-uninstall', [], script_only=True)
    except:
      traceback.print_exc()
      print('Error: pre-uninstall script failed.')
      return False

    filelist_fn = os.path.join(directory, env.INSTALLED_FILES)
    installed_files = []
    if not os.path.isfile(filelist_fn):
      print('  Warning: No `{}` found in package directory'.format(env.INSTALLED_FILES))
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

    deps = manifest.eval_fields(env.cfgvars(dev), 'dependencies', {})
    if deps:
      print('Installing dependencies for "{}"{}...'.format(manifest.identifier,
          ' (dev) ' if dev else ''))
      if not self.install_dependencies(deps, manifest.directory):
        return False

    deps = manifest.eval_fields(env.cfgvars(dev), 'pip_dependencies', {})
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
    for name, req in deps.items():
      if not isinstance(req, manifest.Requirement):
        req = manifest.Requirement.from_line(req)
      try:
        have_package = self.find_package(name, req.internal)
      except PackageNotFound as exc:
        install_deps.append((name, req))
      else:
        if req.type == 'registry':
          if not req.selector(have_package.version):
            print('  Warning: Dependency "{}@{}" unsatisfied, have "{}" installed'
                .format(name, req.selector, have_package.identifier))
          else:
            print('  Skipping satisfied dependency "{}@{}", have "{}" installed'
                .format(name, req.selector, have_package.identifier))
        else:
          # Must be a Git URL or a relative path.
          print('  Skipping "{}" dependency, have "{}" installed'
            .format(req.type, name, have_package.identifier))
        if self.recursive:
          self.install_dependencies_for(have_package)

    if not install_deps:
      return True

    for name, req in install_deps:
      print('  Installing "{}" ({})'.format(name, req))
      if req.type == 'registry':
        if not self.install_from_registry(name, req.selector, private=req.internal, regs=req.registry)[0]:
          return False
      elif req.type == 'git':
        if not self.install_from_git(req.git_url, req.recursive, req.internal)[0]:
          return False
      elif req.type == 'path':
        path = req.path
        if not os.path.isabs(path):
          path = os.path.join(current_dir, path)
        # TODO: Pass `private` to install_from_directory()
        if not self.install_from_directory(path, req.link)[0]:
          return False
      else:
        raise RuntimeError('unexpected dependency data: "{}" -> {!r}'.format(name, req))

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
        self.installed_python_libs[dep_name] = env.get_module_dist_info(dep_name)

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

  def install_from_requirement(self, req, dev=False):
    """
    Installs from a requirement line or object.
    """

    if isinstance(req, six.string_types):
      req = manifest.Requirement.from_line(req)
    req.inherit_values()

    registry = None
    if req.registry:
      registry = RegistryClient(req.registry, req.registry)

    if req.selector:
      return self.install_from_registry(req.name, req.selector, dev=dev,
        registry=registry, internal=req.internal)
    if req.git_url:
      return self.install_from_git(req.git_url, req.recursive, internal=req.internal)
    if req.path:
      if os.path.isfile(req.path):
        if req.link:
          print('Warning: Can not install in develop mode from archive "{}"'
            .format(req.path))
        success, mnf = self.install_from_archive(req.path, dev=dev, internal=req.internal)
      else:
        success, mnf = self.install_from_directory(req.path, req.link, dev=dev, internal=req.internal)
      info = (mnf['name'], mnf['version']) if success else None
      return success, info

  @decorators.finally_()
  def install_from_directory(self, directory, develop=False, dev=False,
      expect=None, movedir=False, internal=False):
    """
    Installs a package from a directory. The directory must have a
    `nodepy.json` file. If *expect* is specified, it must be a tuple of
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
    internal (bool): Install as an internal dependency.

    # Returns
    (success, manifest)
    """

    filename = os.path.normpath(os.path.abspath(os.path.join(directory, PACKAGE_MANIFEST)))

    try:
      manifest = self._load_manifest(filename)
    except (IOError, OSError) as exc:
      if exc.errno == errno.ENOENT:
        print('Error: directory "{}" contains no package manifest'.format(directory))
        return False, None
      raise
    except InvalidPackageManifest as exc:
      print('Error: directory "{}":'.format(directory), exc)
      return False, None

    if expect is not None and (manifest['name'], manifest['version']) != expect:
      print('Error: Expected to install "{}@{}" but got "{}" in "{}"'
          .format(expect[0], expect[1], manifest.identifier, directory))
      return False, manifest

    if internal and self.currently_installing:
      target_dir = os.path.join(self.currently_installing[-1][1], env.MODULES_DIRECTORY, manifest['name'])
      print('Installing "{}" as internal dependency of "{}" ...'.format(
        manifest.identifier, self.currently_installing[-1][0].identifier))
    else:
      print('Installing "{}"...'.format(manifest.identifier))
      target_dir = os.path.join(self.dirs['packages'], manifest['name'])

    self.currently_installing.append((manifest, target_dir))
    decorators.finally_(lambda: self.currently_installing.pop())

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
        print('  Creating {} to "{}"...'.format(env.LINK_FILE, directory))
        linkfn = os.path.join(target_dir, env.LINK_FILE)
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
    for script_name, filename in manifest.get('bin', {}).items():
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
        filename = os.path.abspath(os.path.join(target_dir, filename))
        installed_files += self.script.make_nodepy(
            script_name, filename, self.dirs['reference_dir'])

    # Write down the names of the installed files.
    with open(os.path.join(target_dir, env.INSTALLED_FILES), 'w') as fp:
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

  def install_from_registry(self, package_name, selector, dev=False, regs=None, internal=False):
    """
    Install a package from a registry.

    # Returns
    (success, (package_name, package_version))
    """

    # Check if the package already exists.
    try:
      package = self.find_package(package_name, internal)
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

    if isinstance(regs, six.string_types):
      regs = [_registry.RegistryClient(regs, regs)]
    elif isinstance(regs, _registry.RegistryClient):
      regs = [regs]
    elif regs is None:
      regs = self.reg

    print('Finding package matching "{}@{}"...'.format(package_name, selector))
    for registry in regs:
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
      success = self.install_from_archive(tmp.name, dev=dev, expect=(package_name, info.version), internal=internal)
    finally:
      if tmp and os.path.isfile(tmp.name):
        os.remove(tmp.name)

    return success, (package_name, info.version)

  def install_from_git(self, url, recursive=True, internal=False):
    """
    Install a package from a Git repository. The package will first be cloned
    into a temporary directory, that be copied into the correct location and
    binaries will be installed.

    # Returns
    (success, (package_name, package_version))
    """

    # TODO: Handle `private` argument

    if '@' in url:
      url, ref = url.partition('@')[::2]
    else:
      ref = None

    dest = os.path.join(self.dirs['packages'], '.tmp')
    args = ['git', 'clone', url, dest]
    if ref:
      args += ['-b', ref]
    if recursive:
      args += ['--recursive']
    print('Cloning repository: $', args)
    res = subprocess.call(args)
    if res != 0:
      print('Error: Git clone failed')
      return False, None

    with later(_rmtree, dest):
      success, manifest = self.install_from_directory(dest, movedir=True, internal=internal)

    if manifest:
      return success, (manifest['name'], manifest['version'])
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
