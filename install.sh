ln -sf `pwd`/.vimrc ~/.vimrc
ln -sf `pwd`/.tmux.conf ~/.tmux.conf
ln -sf `pwd`/.tmux.remote.conf ~/.tmux.remote.conf
ln -sf `pwd`/.inputrc ~/.inputrc
ln -sf `pwd`/.config/nvim/ ~/.config/nvim

sudo cp `pwd`/speak_date.sh /usr/local/bin/speak.sh
sudo chmod 777 /usr/local/bin/speak.sh
sudo cp `pwd`/speak_date.sh /usr/local/bin/speak_date.sh
sudo chmod 777 /usr/local/bin/speak_date.sh

# Idempotent append cron job
grep -Fxe 'speak_time.sh' /etc/crontabs/root || {
  sudo tee -a /etc/crontabs/root <<<"0 * * * * sh /usr/local/bin/speak_time.sh"
}
