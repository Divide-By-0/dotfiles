# dotfiles

```
git clone git@github.com:Divide-By-0/dotfiles.git
cd dotfiles
sh install_pm.sh
sh install.sh
vi <somefile>
/PlugInstall
```

:PlugInstall on last step instead on failure.

## macOS agent session recovery

The cmux/tmux/Claude/Codex restart tooling lives in
`macos/agent-session-recovery`. It has its own tested installer because it
also manages LaunchAgents, Claude hooks, and a small tmux-resurrect
compatibility patch:

```sh
cd macos/agent-session-recovery
./tests/run.sh
./install.sh --activate
```
