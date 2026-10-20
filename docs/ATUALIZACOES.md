# Atualizacoes

## Versao 0.1.10

Corrige a cantoria acionada por presentes nas instalacoes Windows. O servidor
agora encontra as musicas instaladas nas subpastas de
`server/assets/singing/processed/FANCLUB`, mantendo compatibilidade com o
formato anterior. Os pacotes de audio existentes sao preservados e nao
precisam ser baixados novamente.

## Versao 0.1.9

O instalador do Windows agora localiza automaticamente o Python 3.12 quando o
instalador oficial retorna codigo zero, mas escolhe a pasta padrao do usuario
em vez do diretorio portatil solicitado. A criacao do ambiente virtual tambem
e validada antes de continuar.

## Versao 0.1.8

O instalador apaga cada pacote temporario de audio assim que termina de
extrai-lo. Isso reduz em varios gigabytes o espaco temporario necessario e
evita o erro `curl (23) Failure writing output to destination` causado por
disco cheio. Antes de baixar e extrair, ele tambem informa claramente quanto
espaco ainda e necessario.

## Versao 0.1.7

O monitor agora reconhece os limites de conexao da Euler Stream e aguarda o
horario informado pelo servidor antes de tentar novamente. O painel mostra o
estado `LIMITE EULER - AGUARDANDO`, e reinicios internos deixam de criar varias
tentativas simultaneas.

Quando esse estado aparecer, mantenha o programa aberto. O monitor retomara
automaticamente; reiniciar o computador ou clicar repetidamente em conectar
nao antecipa a liberacao da cota.

O launcher verifica atualizacoes antes de iniciar o servidor. Por padrao ele usa o manifesto oficial:

```text
https://raw.githubusercontent.com/golgol2/Snake-Reyers/main/update-manifest.json
```

Uma instalacao pode substituir essa URL criando o arquivo:

```text
server/private/update.json
```

Exemplo:

```json
{
  "manifest_url": "https://raw.githubusercontent.com/USUARIO/REPOSITORIO/main/manifest.json"
}
```

O pacote e instalado somente quando a versao e mais nova e o SHA-256 coincide com o manifesto. As pastas `server/private`, `server/runs`, `server/assets` e `server/runtime` sao preservadas.

A partir da versao `0.1.5`, abra o painel e use **Atualizar sistema**. O painel verifica a versao, baixa o pacote correspondente ao sistema operacional, valida o SHA-256, instala os arquivos e reinicia automaticamente. Para proteger a transmissao, a instalacao so e liberada quando a live esta parada.

Quem estiver em uma versao anterior a `0.1.5` precisa iniciar o Snake Reyers uma vez pelo launcher para receber esse botao. Depois disso, as proximas versoes podem ser instaladas diretamente pelo painel.

A partir da versao `0.1.6`, a instalacao e as atualizacoes criam ou reparam o atalho **Snake Reyers** na Area de Trabalho. No Windows, o atalho inicia o servidor sem exibir a janela do PowerShell.

As musicas e reacoes usam um catalogo independente em `content-manifest.json`. Essa verificacao aparece no painel e nunca instala arquivos enquanto uma live esta iniciando, rodando ou finalizando.
