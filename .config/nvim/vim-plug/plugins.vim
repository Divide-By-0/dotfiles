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
                         
    Plug 'easymotion/vim-easymotion'      
    Plug 'preservim/nerdcommenter'                      
    Plug 'preservim/nerdtree'  
    Plug 'vim-airline/vim-airline'
    Plug 'vim-airline/vim-airline-themes'
    Plug 'mhinz/vim-signify'
    Plug 'justinmk/vim-sneak'
    Plug 'unblevable/quick-scope'
    Plug 'junegunn/fzf', { 'do': { -> fzf#install() } }
    Plug 'junegunn/fzf.vim'
    Plug 'airblade/vim-rooter'
    Plug 'fatih/vim-go', { 'do': ':GoUpdateBinaries' }
    Plug 'christoomey/vim-tmux-navigator'
    Plug 'meck/vim-brittany'
    Plug 'neovimhaskell/haskell-vim'
    Plug 'kassio/neoterm'
    " Vim for Chrome textboxes 
    " Plug 'glacambre/firenvim', { 'do': { _ -> firenvim#install(0) } }

call plug#end()
