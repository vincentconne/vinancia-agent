# Composants tiers

Vinancia Agent est publié sous licence MIT (voir `LICENSE`). Les exécutables embarquent notamment :

- **Woob** (<https://woob.tech>) : GNU LGPL v3 ou ultérieure. Ses sources sont
  disponibles sur <https://gitlab.com/woob/woob>, et la version utilisée est indiquée
  dans `requirements.txt`. Tu peux reconstruire l'exécutable avec une autre version
  de Woob à partir des sources de ce dépôt (`python build_agent.py`).
- Les modules bancaires Woob, téléchargés depuis woob.tech après installation (LGPL v3).
- keyring, requests, cryptography, python-jose, chompjs, schwifty, pycryptodome,
  xlrd, lxml, Pillow, PyYAML, Python (licences permissives : MIT, BSD, Apache 2.0, PSF).
