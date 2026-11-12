"""
Construit l'exécutable de Vinancia Agent avec PyInstaller (Windows .exe, macOS .app).

    python -m pip install -r requirements-build.txt
    python build_agent.py

Particularité : les modules bancaires Woob ne sont pas dans l'exécutable. Ils sont
téléchargés (et vérifiés) après installation, puis importés dynamiquement. Ils
peuvent donc importer n'importe quel module de la bibliothèque standard ou de
leurs dépendances : on embarque toute la bibliothèque standard, toutes les
dépendances déclarées, et leurs métadonnées (Woob vérifie les versions requises
par chaque module via importlib.metadata).
"""
import os
import pkgutil
import platform
import sys

import PyInstaller.__main__

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = 'VinanciaAgent'

# Paquets de la bibliothèque standard inutiles pour un module bancaire.
STDLIB_SKIP = {
    'idlelib', 'turtledemo', 'turtle', 'test', 'tests', 'lib2to3', 'ensurepip', 'venv',
    'pydoc_data', 'this', 'antigravity', 'tkinter', 'unittest', 'doctest',
    'distutils', 'msilib', 'nis', 'ossaudiodev', 'spwd', 'crypt', 'sndhdr', 'imghdr',
}
# Dépendances (noms d'import) à embarquer entièrement.
PACKAGES = [
    'woob', 'jose', 'chompjs', 'schwifty', 'Crypto', 'Cryptodome', 'xlrd', 'lxml', 'PIL',
    'dateutil', 'babel', 'pycountry', 'unidecode', 'html2text', 'yaml', 'requests', 'urllib3',
    'certifi', 'charset_normalizer', 'idna', 'keyring', 'cryptography', 'termcolor', 'rich',
    'packaging', 'ecdsa', 'rsa', 'pyasn1', 'webview', 'bottle', 'proxy_tools',
]
# Distributions dont Woob lit la version (requirements.txt des modules bancaires).
METADATA = [
    'woob', 'python-jose', 'chompjs', 'schwifty', 'pycryptodome', 'pycryptodomex', 'xlrd',
    'lxml', 'Pillow', 'python-dateutil', 'Babel', 'pycountry', 'Unidecode', 'html2text',
    'PyYAML', 'requests', 'urllib3', 'keyring', 'cryptography', 'termcolor', 'rich', 'packaging',
    'pywebview',
]


def stdlib_modules():
    names = []
    for name in sorted(sys.stdlib_module_names):
        if name.startswith('_') and name not in ('_strptime', '_datetime', '_decimal', '_pydecimal'):
            continue
        if name in STDLIB_SKIP:
            continue
        if platform.system() != 'Windows' and name in ('winreg', 'winsound', 'msvcrt', '_winapi', 'nt'):
            continue
        if platform.system() == 'Windows' and name in ('pwd', 'grp', 'termios', 'tty', 'pty', 'fcntl',
                                                        'resource', 'syslog', 'posix', 'curses', 'readline'):
            continue
        try:
            module = __import__(name)
        except Exception:
            continue
        names.append(name)
        if hasattr(module, '__path__'):
            for info in pkgutil.walk_packages(module.__path__, prefix=name + '.', onerror=lambda _n: None):
                if any(part in STDLIB_SKIP or part.startswith('test') for part in info.name.split('.')):
                    continue
                names.append(info.name)
    return names


def main():
    system = platform.system()
    args = [
        os.path.join(HERE, 'vinancia_agent.py'),
        '--name', NAME,
        '--windowed',
        '--noconfirm',
        '--clean',
        '--paths', HERE,
        '--add-data', f"{os.path.join(HERE, 'ui')}{os.pathsep}ui",
        '--hidden-import', 'agent_gui',
        '--hidden-import', 'agent_core',
        '--hidden-import', 'agent_payload',
        '--hidden-import', 'pgp_verify',
        '--exclude-module', 'selenium',
        '--exclude-module', 'pytest',
        '--exclude-module', 'tkinter',
    ]
    args += ['--onefile'] if system != 'Darwin' else ['--osx-bundle-identifier', 'com.vinancia.agent']
    # PNG converti par PyInstaller (Pillow) en .ico / .icns.
    args += ['--icon', os.path.join(HERE, 'icon.png')]
    for package in PACKAGES:
        args += ['--collect-all', package]
    # Moteur de la fenêtre : celui du système (WebView2 via pythonnet sous Windows,
    # WebKit via PyObjC sur Mac). Qt ne sert qu'à Linux : exclu ailleurs (−200 Mo).
    native = {'Windows': ['clr', 'clr_loader', 'pythonnet'],
              'Darwin': ['objc', 'AppKit', 'Foundation', 'WebKit', 'Quartz', 'Security', 'UniformTypeIdentifiers']}
    for package in native.get(system, []):
        try:
            __import__(package)
        except Exception:
            continue
        args += ['--collect-all', package]
    if system != 'Linux':
        for qt in ('PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'qtpy', 'gi'):
            args += ['--exclude-module', qt]
    for dist in METADATA:
        args += ['--copy-metadata', dist]
    for name in stdlib_modules():
        args += ['--hidden-import', name]
    PyInstaller.__main__.run(args)


if __name__ == '__main__':
    main()
