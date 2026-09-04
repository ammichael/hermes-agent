# local/patches

Branch de trabalho deste checkout (`~/.hermes/hermes-agent`). Nunca commitar em `main`.

Por quê: `hermes update` (disparado automaticamente pelo Desktop com `--keep-stash`) faz
`git merge --ff-only origin/main`; se `main` diverge, cai em `git reset --hard origin/main`
(`hermes_cli/update_cmd.py`, bloco "reset --hard") e apaga commits locais sem ref de resgate.
Entre 2026-08-04 e 2026-09-03 isso destruiu 33 commits (índice em
`~/.hermes/backups/lost-local-commits/INDEX.md`, branches `lost/*`).

Como funciona: `config.yaml` tem `updates.parked_branch_strategy: update_in_place`. Com o checkout
nesta branch e ao menos um commit não mergeado (este arquivo), o update faz merge de `origin/main`
NA branch em vez de trocar pra `main`. Este arquivo é o commit-âncora; não remover.

Regras:
1. Fix que cabe em `~/.hermes/scripts|hooks|plugins|config.yaml` NÃO entra aqui.
2. Fix que precisa do core: commit aqui + `git format-patch` em `~/.hermes/backups/local-patches/`
   + PR pro upstream a partir do fork (`fork` = ammichael/hermes-agent).
3. Push diário pro fork: `~/.hermes/scripts/hermes-agent-daily-commit-push.py` (branch local/patches).
