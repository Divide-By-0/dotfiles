" auto-install vim-plug
if empty(glob('~/.config/nvim/autoload/plug.vim'))
  silent !curl -fLo ~/.config/nvim/autoload/plug.vim --create-dirs
    \ https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim
  "autocmd VimEnter * PlugInstall
  "autocmd VimEnter * PlugInstall | source $MYVIMRC
endif

call plug#begin('~/.config/nvim/autoload/plugged')

    " Better Syntax Support
    Plug 'sheerun/vim-polyglot'
    " File Explorer
    " Auto pairs for '(' '[' '{'
    Plug 'jiangmiao/auto-pairs'
    Plug 'dkasak/gruvbox'
    Plug 'tpope/vim-fugitive'
    " Stable version of coc
    Plug 'neoclide/coc.nvim', {'branch': 'release'}
    Plug 'vim-airline/vim-airline'
    Plug 'vim-airline/vim-airline-themes'
    " git diff
    Plug 'mhinz/vim-signify'
    " snipe with s{char1}{char2}
    Plug 'justinmk/vim-sneak'
    " highlight before jumping with f
    Plug 'unblevable/quick-scope'
    " quick search
    Plug 'junegunn/fzf', { 'do': { -> fzf#install() } }
    Plug 'junegunn/fzf.vim'
    Plug 'airblade/vim-rooter'
    Plug 'fatih/vim-go', { 'do': ':GoUpdateBinaries' }
    Plug 'christoomey/vim-tmux-navigator'
    Plug 'meck/vim-brittany'
    " better haskell highlighting
    Plug 'neovimhaskell/haskell-ovim'
    " better terminal support in neovim
    Plug 'kassio/neoterm'
    " :BD to kill a buffer befor
    Plug 'qpkorr/vim-bufkill'
call plug#end()
