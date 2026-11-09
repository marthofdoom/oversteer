project('oversteer',
  version: '0.18.0-rc3',
  meson_version: '>= 0.56.0'
)

i18n = import('i18n')
pymod = import('python')
prefix = get_option('prefix')
pkgdatadir = join_paths(prefix, get_option('datadir'), meson.project_name())
py_installation = pymod.find_installation(get_option('python'))
py_path = py_installation.get_install_dir()

# The build id: the commit (short hash, -dirty with local changes). A source tarball or a Flatpak git checkout may have
# no .git: -Dbuild_id=<hash> says it then (the Flatpak manifest does); with neither it is "unknown".
build_id = get_option('build_id')
if build_id == ''
  git = find_program('git', required: false)
  if git.found()
    head = run_command(git, '-C', meson.project_source_root(), 'rev-parse', '--short=9', 'HEAD', check: false)
    if head.returncode() == 0 and head.stdout().strip() != ''
      build_id = head.stdout().strip()
      state = run_command(git, '-C', meson.project_source_root(), 'status', '--porcelain', '--untracked-files=no', check: false)
      if state.returncode() == 0 and state.stdout().strip() != ''
        build_id += '-dirty'
      endif
    endif
  endif
endif
if build_id == ''
  build_id = 'unknown'
endif

python3_required_modules = ['gi', 'pyudev', 'xdg', 'evdev', 'gettext', 'matplotlib', 'scipy', 'numpy']
foreach p : python3_required_modules
  script = 'import importlib.util; import sys; exit(1) if importlib.util.find_spec(\''+ p +'\') is None else exit(0)'
  if run_command(py_installation, '-c', script, check: true).returncode() != 0
    error('Required Python3 module \'' + p + '\' not found')
  endif
endforeach

udev_rules_dir = get_option('udev_rules_dir')
if udev_rules_dir == ''
  if prefix == '/usr/local'
    udev_rules_dir = '/usr/local/lib/udev/rules.d'
  else
    udev_dep = dependency('udev')
    udev_rules_dir = udev_dep.get_variable(pkgconfig: 'udevdir') + '/rules.d'
  endif
elif udev_rules_dir == 'packed'
  udev_rules_dir = pkgdatadir + '/udev'
endif

install_subdir('oversteer', install_dir: py_path)
subdir('data')
subdir('bin')
subdir('po')

meson.add_install_script('scripts/meson_post_install.py')
