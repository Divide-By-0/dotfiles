ln -sf `pwd`/.vimrc ~/.vimrc
ln -sf `pwd`/.tmux.conf ~/.tmux.conf
ln -sf `pwd`/.tmux.remote.conf ~/.tmux.remote.conf
ln -sf `pwd`/.inputrc ~/.inputrc
ln -sf `pwd`/.config/nvim/ ~/.config/nvim

sudo cp `pwd`/speak_date.sh /usr/local/bin/speak.sh
sudo chmod 777 /usr/local/bin/speak.sh
sudo cp `pwd`/speak_time.sh /usr/local/bin/speak_time.sh
sudo chmod 777 /usr/local/bin/speak_time.sh

# Idempotent bash aliases
if [ ! -f ~/.bash_aliases ]
then
    cp .bash_aliases ~/.bash_aliases
else
    echo "Bash aliases not copied over, file already exists."
fi

# Idempotent append cron job
grep -Fxe 'speak_time.sh' /etc/crontab || {
  sudo tee -a /etc/crontab <<< "0 * * * * sh /usr/local/bin/speak_time.sh"
}

# Ergodex EZ training shortcut
sudo cp 50-oryx.rules /etc/udev/rules.d/50-oryx.rules
sudo groupadd plugdev
sudo usermod -aG plugdev $USER
