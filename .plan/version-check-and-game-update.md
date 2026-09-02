# Plano: verificação da CLI e atualização de instalações de jogos

## Objetivo

Entregar dois mecanismos independentes:

1. Avisar, no máximo uma vez a cada 24 horas, quando existir uma versão mais nova do próprio `dlss5-enabler`.
2. Registrar corretamente a versão da ferramenta e as opções da instalação em cada jogo, permitindo reaplicar a mesma configuração quando o usuário atualizar o `dlss5-enabler`.

O primeiro mecanismo apenas recomenda como atualizar a CLI. O segundo atualiza os arquivos administrados dentro de um jogo. Eles não compartilham estado nem responsabilidades.

## Estado atual confirmado

- `pyproject.toml` declara a versão publicada atual.
- `dlss5_enabler/__init__.py` ainda contém `__version__ = "1.0.0"` hardcoded.
- `InstallRecord.tool_version` e `IndexEntry.tool_version` também usam `"1.0.0"` hardcoded.
- O arquivo `dlss5-enabler.install.json` já é salvo ao lado do executável do jogo.
- O registro por jogo já contém arquitetura, modo gráfico, flags de D3D9, OpenGL, Vulkan e LumeniteFX.
- O registro também contém versão, hash, tamanho e URL dos binários instalados.
- O índice global replica parte desse metadata para o comando `list`.
- O comando `info` lê o registro por jogo, mas ainda não mostra `tool_version`.
- Uma reinstalação administrada já cria snapshot, remove a instalação anterior, instala novamente e restaura o snapshot se houver falha.

O primeiro passo da implementação deve eliminar as versões duplicadas. Nenhum modelo deve voltar a declarar uma versão literal.

## Separação obrigatória

| Mecanismo | Estado persistente | Origem da verdade | Efeito |
| --- | --- | --- | --- |
| Check da CLI | Somente um marcador temporal vazio no cache | Versão instalada e PyPI | Mostra recomendação; nunca atualiza automaticamente |
| Update do jogo | `dlss5-enabler.install.json` e índice global | Metadata da instalação anterior e versão atual da CLI | Reexecuta a instalação transacional com as mesmas opções |

O marcador de 24 horas não guarda versão local, versão remota, resposta HTTP ou JSON. O registro do jogo não controla quando a CLI consulta o PyPI.

## Parte 1: fonte única da versão da ferramenta

### Versão em runtime

Criar um pequeno módulo tipado para obter a versão instalada por `importlib.metadata.version("dlss5-enabler")`.

- `pyproject.toml` continua sendo a versão declarada para build e publicação.
- `dlss5_enabler.__version__` passa a expor o resultado do helper, sem literal duplicado.
- `InstallRecord.tool_version` usa `default_factory` com o mesmo helper.
- `IndexEntry.tool_version` usa o valor recebido do `InstallRecord`; seu fallback de compatibilidade também deve vir do helper ou de uma migração explícita.
- Se o pacote não possuir metadata instalável, o helper retorna um identificador neutro como `0+unknown`, sem inventar a versão do `pyproject.toml`.
- Comparações usam `packaging.version.Version`; `packaging` deve ser dependência direta, não apenas transitiva.

Adicionar um teste que procure versões literais duplicadas nos modelos e impeça nova divergência.

### Compatibilidade com registros existentes

Registros criados por `v1.0.1` podem declarar incorretamente `tool_version: "1.0.0"`. Eles devem continuar carregando normalmente e ser considerados instalações antigas.

Não reescrever um registro apenas ao consultá-lo. A versão e o schema são atualizados somente depois de uma atualização ou reinstalação concluída com sucesso.

## Parte 2: check da versão da CLI a cada 24 horas

### Origem remota

Usar o endpoint público do PyPI para `dlss5-enabler`, pois tanto `uv` quanto `pip` instalam a distribuição publicada ali.

- Ler apenas `info.version` da resposta.
- Validar o formato antes de comparar.
- Ignorar prereleases para uma instalação estável.
- Permitir que uma instalação prerelease reconheça uma prerelease mais nova segundo as regras de `packaging.version`.
- Nunca baixar ou executar código durante o check.

### Marcador temporal, sem cache de versão

Criar um arquivo vazio no diretório de cache, por exemplo:

```text
<cache>/update-check.lock
```

Regras:

- O conteúdo deve permanecer vazio.
- O `mtime` representa quando a última tentativa de check terminou.
- Se o arquivo não existir ou tiver pelo menos 24 horas, consultar o PyPI.
- Se tiver menos de 24 horas, não fazer nenhuma requisição.
- Atualizar o `mtime` depois de uma tentativa concluída, com ou sem sucesso, para uma indisponibilidade do PyPI não atrasar todos os comandos seguintes.
- Se o relógio do arquivo estiver muito no futuro, considerar o marcador inválido e permitir um novo check.
- `cache --clean` pode remover o marcador; o próximo comando fará uma nova verificação.

O marcador precisa de exclusão mútua, mas continua sem metadata:

1. Adquirir `resource_lock()` usando o caminho do marcador como identidade.
2. Reavaliar o `mtime` depois de obter o lock.
3. Somente o primeiro processo vencido consulta a rede.
4. Os demais observam o marcador renovado e continuam sem outra requisição.

Não usar JSON para esse mecanismo.

### Limites e falhas

O check é informativo e nunca pode impedir `install`, `update`, `info`, `list` ou `uninstall`.

- Uma única tentativa HTTP.
- Timeout curto e próprio, sem fallback para curl.
- Orçamento total recomendado de até cinco segundos.
- Resposta inválida, ausência de rede, erro TLS ou timeout não geram traceback para o usuário.
- Em modo verbose, registrar a razão da falha.
- Renovar o marcador mesmo quando a tentativa falhar.
- Não confundir essa falha com as regras mais rigorosas dos downloads necessários para instalar o jogo.

### Pontos de execução

Executar o check informativo antes dos comandos normais de gerenciamento em que o aviso é útil:

- `install`;
- `update`;
- `info`;
- `list`.

Não consultar automaticamente durante:

- `check`, porque CI e validação local não dependem de rede;
- `uninstall`, porque remoção e recuperação devem funcionar sem atraso externo;
- `cache --clean`;
- `--help`.

Adicionar `dlss5-enabler version` para mostrar sempre a versão local sem rede. A opção `dlss5-enabler version --check` força uma consulta explícita, ignora a janela atual e renova o marcador.

### Saída quando houver versão nova

Quando `latest > current`, mostrar um warning curto e continuar o comando solicitado:

```text
DLSS5 Enabler 1.2.0 is available; you are running 1.1.0.
Update with: uv tool upgrade dlss5-enabler
pip alternative: python -m pip install --upgrade dlss5-enabler
```

Não tentar detectar de forma frágil qual gerenciador instalou o pacote. Mostrar `uv` primeiro e `pip` como alternativa.

Quando a versão for igual, mais antiga ou inválida, não mostrar recomendação de upgrade.

## Parte 3: metadata completo por jogo

### Schema do registro

Adicionar `schema_version` ao `InstallRecord` e registrar explicitamente as opções solicitadas na primeira instalação.

Modelo conceitual:

```json
{
  "schema_version": 2,
  "tool_version": "1.1.0",
  "game_exe": "C:/Games/Example/game.exe",
  "install_options": {
    "lumenite": true,
    "d3d9": false,
    "opengl": false,
    "vulkan_layer": true
  }
}
```

Continuar mantendo os campos de resultado necessários para uninstall, diagnóstico e compatibilidade. Não salvar uma string de linha de comando; salvar opções tipadas e reconstruir a chamada a partir delas.

A separação entre opção solicitada e resultado instalado é importante. Por exemplo, uma versão pode solicitar Vulkan quando o upstream ainda não fornece a camada; uma versão posterior deve poder tentar novamente a opção originalmente escolhida.

### Migração de registros antigos

Ao carregar um registro sem `install_options`, derivar as opções dos campos existentes:

- `lumenite = lumenite_installed`;
- `d3d9 = d3d9_translate`;
- `opengl = opengl`;
- `vulkan_layer = vulkan_layer`.

Essa derivação é somente em memória até uma instalação bem-sucedida. Registros inválidos ou incompletos devem gerar erro claro e nunca ser sobrescritos.

O índice global deve guardar pelo menos `tool_version` e `schema_version`, mas o registro por jogo continua sendo a autoridade para executar um update.

### Exibição

Atualizar `info` para mostrar:

- versão que instalou o jogo;
- versão atualmente executada;
- opções salvas;
- status `Current`, `Update available`, `Newer than this CLI` ou `Unknown legacy version`;
- versões dos componentes já existentes em `binaries`.

Atualizar `list` com colunas compactas de versão instalada e status local. Esse status compara somente a versão do registro com a CLI em execução e não consulta a rede por jogo.

## Parte 4: comando de atualização do jogo

### Interface inicial

Adicionar:

```console
dlss5-enabler update "/path/to/game.exe"
```

O alvo pode ser o executável registrado ou o diretório do jogo. O comando deve localizar `dlss5-enabler.install.json`, validar o registro e usar `game_exe` salvo quando o alvo for um diretório.

Opções:

- `--reinstall`: reaplicar mesmo quando `tool_version` já for igual à versão atual;
- `--force-download`: invalidar o cache de componentes, mantendo o significado existente;
- `--verbose`: manter o comportamento atual de logs.

Não adicionar `update --all` na primeira implementação. Atualização em massa deve ser uma extensão separada depois que o fluxo de um único jogo estiver estável.

### Decisão de versão

- Se a versão do registro for menor que a CLI atual, permitir update.
- Se for igual, informar que já está atualizado e não modificar nada, salvo `--reinstall`.
- Se o registro for mais novo que a CLI, recusar downgrade e recomendar atualizar a CLI.
- Se a versão for desconhecida ou legado inválido, permitir update somente depois de validar e exibir as opções reconstruídas.
- Uma alteração de engine, DLL fixada, formato ou comportamento que precise chegar aos jogos existentes exige incremento da versão geral do pacote.

### Reaplicação da instalação

O comando não terá um segundo instalador. Ele deve chamar a mesma orquestração usada por `install`, passando as opções estruturadas do registro anterior:

- `install_lumenite`;
- `d3d9_translate`;
- `opengl`;
- `install_vulkan_layer`;
- `force_download` somente quando solicitado no novo comando.

Fluxo:

1. Obter o lock da operação do jogo.
2. Carregar e validar o registro existente.
3. Comparar versões e reconstruir as opções.
4. Mostrar versão anterior, versão nova e opções que serão reaplicadas.
5. Resolver e validar todos os componentes necessários antes de modificar o jogo.
6. Criar snapshot recuperável da instalação atual.
7. Remover somente as mutações registradas.
8. Reinstalar usando o mesmo pipeline e as opções salvas.
9. Em sucesso, salvar o novo `schema_version`, `tool_version`, timestamp, opções e metadata dos binários.
10. Em falha, restaurar integralmente o snapshot e manter o registro anterior.

O lock não pode ser adquirido duas vezes pelo mesmo fluxo. Refatorar `run_install` para aceitar um contexto de operação já bloqueado ou criar uma função interna que `install` e `update` chamem sob um único lock.

O requisito de resolver componentes antes de remover a instalação deve ser coordenado com a evolução do pipeline de upstreams. Até isso existir, o update não está pronto para release mesmo que o rollback atual funcione.

## Parte 5: revisão do README e instalação via pip

Manter `uv` como ferramenta obrigatória para desenvolvimento e como recomendação principal para usuários. O caminho mais simples para um usuário leigo deve executar explicitamente a versão mais recente:

```console
uvx dlss5-enabler@latest --help
uvx dlss5-enabler@latest info "/path/to/game.exe"
uvx dlss5-enabler@latest install "/path/to/game.exe"
```

A sintaxe `command@latest` é suportada oficialmente pelo `uvx` e força refresh da versão resolvida. Usá-la nos exemplos de execução efêmera evita que um ambiente antigo instalado ou em cache seja escolhido implicitamente.

Para uso frequente, mostrar a instalação persistente recomendada:

```console
uv tool install dlss5-enabler@latest
dlss5-enabler --help
```

Explicar em uma única frase que uma instalação persistente é atualizada com:

```console
uv tool upgrade dlss5-enabler
```

Logo depois, adicionar o heading `### Install with pip` e somente este bloco curto:

```console
python -m pip install --upgrade dlss5-enabler
dlss5-enabler --help
```

Usar `--upgrade` também na instalação inicial é válido e evita manter uma instalação pip antiga quando o usuário repetir o comando.

Não duplicar a referência da CLI. Os mesmos comandos `dlss5-enabler ...` funcionam depois de uma instalação persistente por `uv` ou `pip`.

Fazer uma leitura completa do README durante a implementação e corrigir a separação entre públicos:

- exemplos para usuários leigos usam `uvx dlss5-enabler@latest ...` ou o executável persistente `dlss5-enabler`;
- `uv run dlss5-enabler ...` aparece somente na seção de checkout/desenvolvimento;
- a referência geral de comandos usa o executável neutro `dlss5-enabler`, para servir a uv e pip;
- Requirements apresenta `uv` como recomendado e `pip` como suportado, sem afirmar que uv é necessário para quem instalou pelo pip;
- o badge com quantidade hardcoded de testes é removido ou substituído por um badge dinâmico, pois já está desatualizado;
- exemplos de Windows, Linux, Proton, update e uninstall continuam coerentes depois da reorganização;
- nenhuma seção induz o usuário a misturar `pip` dentro de um ambiente criado por `uv tool`.

Também documentar de forma breve:

- o aviso automático de versão a cada 24 horas;
- que ele não atualiza nada sozinho;
- como atualizar a CLI com `uv` ou `pip`;
- que `update GAME` reaplica as opções registradas da instalação anterior.

## Arquivos previstos

- `dlss5_enabler/core/version.py`: versão em runtime e comparação tipada.
- `dlss5_enabler/network/update_check.py`: consulta curta ao PyPI e marcador de 24 horas.
- `dlss5_enabler/core/record.py`: schema, versão real e opções estruturadas.
- `dlss5_enabler/operations/install.py`: entrada compartilhada para install/update sem lock duplicado.
- `dlss5_enabler/operations/steps.py`: preenchimento do novo metadata e ordem segura quando necessário.
- `dlss5_enabler/cli.py`: hooks do check, `version`, `update`, `info` e `list`.
- `pyproject.toml` e `uv.lock`: dependência direta de versionamento e incremento da versão.
- `README.md`: instalação mínima com pip e comandos de atualização.
- testes específicos para versão, update check, records, CLI e pipeline.

Os nomes dos módulos podem ser ajustados durante a implementação se a separação de responsabilidades permanecer igual.

## Matriz de testes

### Fonte da versão

- metadata da distribuição presente;
- metadata ausente retorna versão desconhecida;
- `__version__`, novo `InstallRecord` e índice usam a mesma versão;
- registro legado `1.0.0` continua carregando;
- versões PEP 440 são comparadas corretamente;
- versão remota inválida é rejeitada sem falhar o comando.

### Marcador de 24 horas

- marcador ausente faz exatamente uma requisição;
- marcador com menos de 24 horas não faz requisição;
- marcador com exatamente 24 horas permite requisição;
- marcador vazio continua vazio depois do check;
- o check não grava versão local ou remota;
- sucesso renova o `mtime`;
- timeout, erro HTTP e JSON inválido também renovam o `mtime` e não bloqueiam a CLI;
- relógio do marcador no futuro é tratado com segurança;
- dois processos ou threads concorrentes produzem no máximo uma requisição;
- `version --check` ignora a janela e renova o marcador;
- `check`, `uninstall`, `cache --clean` e `--help` não acessam a rede automaticamente.

### Recomendação da CLI

- latest maior mostra as duas recomendações de upgrade;
- latest igual não mostra warning;
- latest menor não recomenda downgrade;
- prerelease estável é ignorada para usuário estável;
- falha do checker não altera exit code do comando principal;
- nenhum teste padrão acessa o PyPI real.

### Metadata e update do jogo

- primeira instalação salva a versão real e todas as opções solicitadas;
- `info` mostra versões e opções;
- `list` mostra status sem fazer uma consulta por jogo;
- migração em memória reconstrói opções de registro antigo;
- update de D3D11/D3D12 repete opções exatamente;
- update de D3D9 repete dgVoodoo e arquitetura correta;
- update OpenGL preserva o hook escolhido;
- update Vulkan preserva a intenção original;
- update com e sem Lumenite preserva a escolha;
- versão igual não modifica arquivos;
- `--reinstall` reaplica versão igual;
- registro mais novo impede downgrade;
- registro ausente recomenda `install`;
- registro corrompido é preservado;
- falha de download antes da aplicação não modifica o jogo;
- falha durante a instalação restaura arquivos, INIs, registro Wine e metadata anteriores byte a byte;
- sucesso atualiza registro e índice com a versão nova;
- segunda execução é idempotente;
- lock impede install e update simultâneos no mesmo jogo.

### Empacotamento e plataformas

- wheel e sdist expõem a versão correta;
- instalação isolada via `uv tool` executa `--help` e `version`;
- instalação isolada via pip executa `--help` e `version`;
- testes sintéticos do update passam em Linux, macOS e Windows;
- nenhum teste unitário executa DLL, addon, ReShade ou jogo real.

## Fases de implementação

### Fase 1: versão única e registro

- Criar helper de versão.
- Remover os três literals `1.0.0`.
- Adicionar schema e opções estruturadas.
- Implementar leitura compatível de registros antigos.
- Exibir versão atual e instalada no `info` e `list`.

### Fase 2: checker informativo

- Criar consulta curta ao PyPI.
- Criar marcador vazio de 24 horas sob lock.
- Integrar nos comandos permitidos.
- Adicionar `version` e `version --check`.
- Cobrir concorrência, falhas e ausência total de cache de versão.

### Fase 3: update transacional do jogo

- Extrair entrada interna compartilhada do instalador.
- Implementar reconstrução das opções.
- Adicionar comando `update`.
- Garantir resolução completa antes de qualquer mutação.
- Cobrir sucesso, rollback, downgrade, reinstall e idempotência.

### Fase 4: documentação e empacotamento

- Tornar `uvx dlss5-enabler@latest` o quick start recomendado para execução efêmera.
- Documentar instalação persistente com `uv tool` e a seção mínima de pip.
- Revisar o README inteiro, remover contagens hardcoded e separar comandos de usuário dos comandos de desenvolvimento.
- Documentar check e update sem duplicar a referência da CLI.
- Construir e inspecionar wheel e sdist.
- Testar instalações isoladas com uv e pip.

## Critérios de aceite

- Existe uma única fonte de versão em runtime, derivada da metadata instalada.
- Nenhum `InstallRecord` novo recebe uma versão literal desatualizada.
- O checker nunca consulta a rede mais de uma vez dentro de 24 horas por cache compartilhado.
- O arquivo temporal do checker é vazio e não armazena versões.
- Indisponibilidade do PyPI nunca impede outro comando.
- Uma versão nova produz recomendação clara para uv e pip, sem auto-update.
- O registro por jogo guarda a versão real e opções solicitadas tipadas.
- `update GAME` reaplica exatamente as opções anteriores pela mesma lógica de instalação.
- Uma falha preserva integralmente a instalação e o metadata anteriores.
- `info` e `list` distinguem instalação atual, antiga, futura e legado desconhecido.
- README recomenda `uvx dlss5-enabler@latest` para usuários leigos e explica a instalação persistente com uv.
- README contém uma seção de pip curta e funcional, sem duplicar a referência de comandos.
- README reserva `uv run` para desenvolvimento e não contém contagem manual desatualizada de testes.
- A suíte padrão não acessa a rede nem executa binários reais.
- `uv run dlss5-enabler check` e `uv build` passam.
- A matriz CI passa em todos os sistemas e versões de Python suportados.
- O commit e a futura tag de release são assinados.
