#!/usr/bin/env python3
"""
Vinancia Agent : synchro bancaire automatique, depuis TON ordinateur.

Sans argument (double-clic), ouvre la fenêtre de l'application. Tes identifiants
bancaires sont rangés dans le trousseau de ton système et ne quittent jamais ta
machine ; seules tes opérations sont envoyées à Vinancia.

Ligne de commande (utilisateurs avancés) :
  connect [--token]   relie l'agent à ton compte Vinancia
  add                 ajoute une banque
  list                liste les banques
  remove NOM          retire une banque
  sync                synchronise maintenant
  sync --auto         synchro planifiée, sans aucune question (utilisée par la tâche planifiée)
  schedule [--remove] programme (ou retire) la synchro automatique
"""
import argparse
import getpass
import os
import sys
import time

# Application fenêtrée (Windows) : pas de console, donc pas de sys.stdout.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_core as core  # noqa: E402


class TerminalInteraction(core.Interaction):
    def ask(self, field):
        return ask_value(field, prompt=field.label or field.description or 'Code reçu')

    def app_validation(self, message):
        print(f'\n📱 {message}')
        input('Une fois validé sur ton téléphone, appuie sur Entrée… ')

    def redirect(self, url):
        print('Ouvre cette adresse dans ton navigateur et connecte-toi :')
        print(url)
        return input("Colle ici l'adresse finale : ").strip()


def ask_value(value, prompt=None):
    label = prompt or value.label or value.description or value.id
    if value.choices:
        items = list(value.choices.items())
        print(label)
        for i, (_, text) in enumerate(items, 1):
            print(f'  {i:>2}. {text}')
        while True:
            answer = input('Numéro : ').strip()
            if answer.isdigit() and 1 <= int(answer) <= len(items):
                return items[int(answer) - 1][0]
            if not answer and value.default:
                return value.default
    reader = getpass.getpass if value.masked else input
    suffix = f' [{value.default}]' if value.default else ''
    while True:
        answer = reader(f'{label}{suffix} : ').strip()
        if not answer:
            if value.default is not None or not value.required:
                return value.default or ''
            continue
        try:
            value.check_valid(answer)
        except ValueError as error:
            print(f'  Format invalide : {error}')
            continue
        return answer


def cmd_connect(args):
    cfg = core.load_config()
    if args.api_url:
        cfg['api_url'] = args.api_url.rstrip('/')
    if args.token:
        info = core.connect_with_token(cfg, getpass.getpass('Colle le jeton de l’agent (vna_…) : ').strip())
    else:
        pairing = core.start_pairing(cfg)
        print('Une page Vinancia s’est ouverte dans ton navigateur : clique sur « Autoriser ».')
        print(f"Code affiché : {pairing['user_code']}   ({core.pairing_url(pairing['user_code'])})")
        deadline = time.time() + pairing['expires_in']
        info = None
        while info is None and time.time() < deadline:
            time.sleep(pairing.get('interval', 3))
            info = core.finish_pairing(cfg, pairing['device_code'])
        if info is None:
            raise core.AgentError('Le code a expiré : relance « connect ».')
    print(f"✅ Agent relié au compte {info.get('user')}.")


def cmd_add(args):
    core.check_keyring()
    cfg = core.load_config()
    print('Banques courantes :')
    for i, (_, name) in enumerate(core.COMMON_BANKS, 1):
        print(f'  {i:>2}. {name}')
    print('  (ou tape directement le nom d’un module Woob)')
    choice = input('Ta banque : ').strip()
    banks = core.COMMON_BANKS
    module = banks[int(choice) - 1][0] if choice.isdigit() and 1 <= int(choice) <= len(banks) else choice.lower()

    w = core.make_woob()
    print('Préparation du module bancaire…')
    fields = core.bank_fields(core.ensure_module(w, module))
    if core.BANK_HINTS.get(module):
        print(core.BANK_HINTS[module])
    login, password, params = core.split_credentials({field.id: ask_value(field) for field in fields})
    params, secrets = core.separate_secrets(params, fields)
    print('Connexion de test…')
    bank = core.new_bank_entry(cfg, module, login, params, secrets)
    accounts = core.test_bank(w, bank, password, TerminalInteraction())
    core.add_bank(cfg, bank, password)
    print(f'✅ {len(accounts)} compte(s) trouvé(s) :')
    for account in accounts:
        print(f'   - {account.label}')
    print('Lance « sync » pour les envoyer à Vinancia.')


def cmd_list(args):
    cfg = core.load_config()
    if not cfg['banks']:
        print('Aucune banque configurée. Lance « add ».')
    for bank in cfg['banks']:
        status = cfg['status'].get(bank['name'], {})
        print(f"- {bank['name']} ({core.bank_display_name(bank)}) — dernière synchro : "
              f"{cfg['last_sync'].get(bank['name'], 'jamais')} {status.get('message') or ''}")


def cmd_remove(args):
    cfg = core.load_config()
    if args.name not in {b['name'] for b in cfg['banks']}:
        raise core.AgentError(f'Banque inconnue : {args.name}')
    core.remove_bank(cfg, args.name)
    print(f'Banque {args.name} retirée. Les données déjà envoyées restent dans Vinancia.')


def cmd_sync(args):
    interactive = not args.auto and sys.stdin is not None and sys.stdin.isatty()
    cfg = core.load_config()
    if not cfg['banks']:
        raise core.AgentError('Aucune banque configurée.')
    result, report = core.run_sync(
        cfg,
        interaction=TerminalInteraction() if interactive else None,
        progress=print if interactive else None,
    )
    if interactive:
        print(f"✅ {result.get('expenses_created', 0)} dépense(s), {result.get('revenues_created', 0)} revenu(s) "
              f"envoyé(s), {result.get('duplicates_skipped', 0)} déjà connue(s).")
        for item in report:
            if item['status'] != 'ok':
                print(f"⚠️  {item['bank']} : {item['message']}")
    if any(item['status'] != 'ok' for item in report):
        sys.exit(2)


def cmd_schedule(args):
    if args.remove:
        core.remove_schedule()
        print('Synchro automatique retirée.')
    else:
        core.install_schedule()
        print(f'✅ Synchro automatique programmée toutes les {core.SYNC_EVERY_HOURS} h.')


def cmd_selftest(args):
    """Vérifie que l'exécutable embarque tout ce qu'il faut (utilisé par la CI)."""
    import tempfile
    lines, ok = [], True
    os.environ['XDG_CONFIG_HOME'] = tempfile.mkdtemp()
    os.environ['APPDATA'] = os.environ['XDG_CONFIG_HOME']
    import agent_gui
    logos = [f for f in os.listdir(agent_gui.LOGO_DIR) if f.endswith('.png')] if os.path.isdir(agent_gui.LOGO_DIR) else []
    has_page = all(os.path.exists(os.path.join(agent_gui.UI_DIR, f)) for f in ('index.html', 'app.js', 'app.css'))
    ok = ok and len(logos) >= 15 and has_page
    lines.append(f'interface : {"OK" if has_page else "ABSENTE"}, logos : {len(logos)}')
    try:
        import webview  # noqa: F401
        lines.append('pywebview OK')
    except Exception as e:
        ok = False
        lines.append(f'pywebview ERREUR {e}')
    try:
        core.make_storage(os.path.join(os.environ['XDG_CONFIG_HOME'], 'probe.enc'))
        lines.append('stockage chiffré OK')
    except Exception as e:
        lines.append(f'stockage chiffré non testé ({type(e).__name__}: {e})')
    from woob.core import Woob
    from woob.core.repositories import DEFAULT_SOURCES_LIST
    work = tempfile.mkdtemp()
    with open(os.path.join(work, 'sources.list'), 'w') as f:
        f.write(DEFAULT_SOURCES_LIST)
    core.use_builtin_signature_check()
    lines.append('signatures des modules : vérification intégrée')
    w = Woob(workdir=work, datadir=work)
    for module in args.modules or ['boursorama', 'bnp', 'cragr', 'caissedepargne', 'societegenerale',
                                   'creditmutuel', 'lcl', 'bp', 'fortuneo', 'n26', 'degiro', 'boursedirect']:
        try:
            core.bank_fields(core.ensure_module(w, module))
            lines.append(f'{module} OK')
        except Exception as e:
            ok = False
            lines.append(f'{module} ERREUR {type(e).__name__}: {e}')
    text = '\n'.join(lines + ['RESULTAT ' + ('OK' if ok else 'ECHEC')])
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(text)
    print(text)
    sys.exit(0 if ok else 1)


def main():
    if len(sys.argv) == 1 or (sys.argv[1].startswith('-psn_')):  # double-clic (macOS ajoute -psn_…)
        import agent_gui
        agent_gui.main()
        return

    parser = argparse.ArgumentParser(description='Vinancia Agent : synchro bancaire depuis ton ordinateur.')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('connect', help='relier l’agent à ton compte Vinancia')
    p.add_argument('--token', action='store_true', help='coller un jeton au lieu d’autoriser dans le navigateur')
    p.add_argument('--api-url', help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_connect)
    sub.add_parser('add', help='ajouter une banque').set_defaults(func=cmd_add)
    sub.add_parser('list', help='lister les banques').set_defaults(func=cmd_list)
    p = sub.add_parser('remove', help='retirer une banque')
    p.add_argument('name')
    p.set_defaults(func=cmd_remove)
    p = sub.add_parser('sync', help='synchroniser maintenant')
    p.add_argument('--auto', action='store_true', help='mode planifié, sans aucune question')
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser('schedule', help='programmer la synchro automatique')
    p.add_argument('--remove', action='store_true')
    p.set_defaults(func=cmd_schedule)
    p = sub.add_parser('selftest', help=argparse.SUPPRESS)
    p.add_argument('--out')
    p.add_argument('modules', nargs='*')
    p.set_defaults(func=cmd_selftest)
    args = parser.parse_args()

    core.setup_logging(to_file=getattr(args, 'auto', False))
    try:
        args.func(args)
    except core.AgentError as error:
        if getattr(args, 'auto', False):
            core.log.warning('%s', error)
            sys.exit(1)
        sys.exit(f'❌ {error}')
    except KeyboardInterrupt:
        sys.exit('\nInterrompu.')


if __name__ == '__main__':
    main()
