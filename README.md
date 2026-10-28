# Vinancia Agent

Synchronise automatiquement tes comptes bancaires avec ton budget Vinancia, depuis
**ton** ordinateur. Gratuit.

## Installation (Windows, Mac)

1. Télécharge l'application sur <https://vinancia.com/agent>.
2. Ouvre-la.
   - **Windows** : si « Windows a protégé votre ordinateur » s'affiche, clique sur
     « Informations complémentaires », puis « Exécuter quand même ».
   - **Mac** : ouvre le .zip, puis fais un clic droit sur Vinancia Agent → « Ouvrir »
     (la première fois seulement).
3. Clique sur **« Relier à mon compte Vinancia »**. Une page Vinancia s'ouvre :
   vérifie que le code est le même que dans l'application, puis clique sur **« Autoriser »**.
4. Choisis ta banque et saisis tes identifiants. Si ta banque le demande, saisis le
   code reçu par SMS ou valide sur ton téléphone.

C'est tout. L'application programme elle-même la synchro automatique toutes les
6 heures, même fenêtre fermée (Planificateur de tâches sous Windows, LaunchAgent
sur Mac). Elle s'installe dans `%LOCALAPPDATA%\Programs\VinanciaAgent` (Windows,
avec un raccourci dans le menu Démarrer) ou `~/Applications` (Mac).

Environ tous les 90 jours, la banque redemande une validation : c'est la règle
DSP2. Vinancia t'envoie alors une notification ; il suffit de rouvrir l'application
et de cliquer sur « Synchroniser maintenant ».

## Sécurité

- **Identifiants bancaires** : dans le trousseau de ton système (Gestionnaire
  d'identifiants Windows, Trousseau macOS). Ils ne quittent jamais ton ordinateur.
- **Sessions bancaires** (ce qui évite de refaire la validation à chaque fois) :
  chiffrées sur disque, avec une clé rangée dans le trousseau.
- **Modules bancaires Woob** : téléchargés depuis woob.tech et vérifiés par
  signature. La clé de signature est épinglée dans l'application.
- **Données envoyées** : comptes, soldes et opérations, sans numéro de compte ni IBAN
  (chaque compte est désigné par une empreinte opaque).
- **Accès de l'application** : elle ne peut qu'envoyer des opérations, pas lire tes
  données Vinancia. Tu peux la délier depuis Vinancia (Budget → Synchro bancaire) ou
  depuis l'application (« Délier cet ordinateur »).
- **Versions** : chaque version est construite par GitHub Actions à partir des
  sources de ce dépôt et publiée avec ses empreintes SHA-256 (`SHA256SUMS.txt`).

## Linux et ligne de commande

```
python -m pip install -r requirements.txt
python vinancia_agent.py              # ouvre la fenêtre (pywebview : GTK ou Qt requis sous Linux)
python vinancia_agent.py connect      # ou en ligne de commande
python vinancia_agent.py add
python vinancia_agent.py sync
python vinancia_agent.py schedule     # synchro automatique (crontab)
```

Pour construire l'exécutable soi-même : `pip install -r requirements-build.txt`,
puis `python build_agent.py`.

Le dossier de l'application contient la configuration et les sessions chiffrées :

- Windows : `%APPDATA%\VinanciaAgent`
- macOS : `~/Library/Application Support/VinanciaAgent`
- Linux : `~/.config/vinancia-agent`

« Délier cet ordinateur » efface tout (mots de passe, sessions, planification).
