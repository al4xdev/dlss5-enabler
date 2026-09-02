# Plano: resolução robusta de upstreams e testes sintéticos

## Objetivo

Substituir a descoberta otimista de downloads por uma resolução validada, previsível e observável. Cada componente deve tentar a versão mais recente compatível, recorrer a uma versão estável embarcada no wheel quando necessário e abortar antes de alterar a instalação se nenhuma opção puder ser validada.

Esta implementação deve partir do hotfix `v1.0.1`, preservando os limites de timeout, a classificação de erros HTTP e o adaptador GitHub já existentes.

## Invariantes

- Nenhum caminho descoberto dinamicamente é aceito apenas pelo nome ou pela resposta HTTP.
- Todo download ocorre em arquivo temporário e só entra no cache depois da validação.
- Um fallback fixado sempre exige SHA-256 exato.
- Um arquivo compactado sempre exige validação dos membros obrigatórios e rejeição de caminhos inseguros ou colisões.
- Falhas de descoberta, download, integridade ou compatibilidade geram mensagens diferentes.
- O fallback nunca é silencioso.
- Todos os upstreams são resolvidos antes da remoção ou modificação de uma instalação existente.
- Se latest e fallback falharem, nenhum arquivo do jogo nem registro Wine é modificado.
- Testes unitários não acessam a rede e não executam binários de terceiros.

## Manifesto embarcado

Criar `dlss5_enabler/upstreams.json` e carregá-lo como recurso do pacote, sem depender do diretório de trabalho. Confirmar no teste do wheel que o arquivo foi incluído pela configuração do Hatchling.

O documento terá uma versão global de schema e uma entrada por componente. O formato inicial deve suportar release assets, arquivos de repositório, archives de snapshots e URLs oficiais externas.

Exemplo estrutural:

```json
{
  "schema_version": 1,
  "components": {
    "feeder": {
      "provider": "github",
      "repository": "jlrouzies-fr/DLSS5-Feeder",
      "discovery": {
        "kind": "latest_release",
        "asset_patterns": ["DLSS5-Feeder-*.zip"]
      },
      "stable": {
        "tag": "v0.12.0",
        "asset_id": 541386212,
        "asset_name": "DLSS5-Feeder-0.12.0.zip",
        "url": "https://github.com/jlrouzies-fr/DLSS5-Feeder/releases/download/v0.12.0/DLSS5-Feeder-0.12.0.zip",
        "sha256": "<sha256-verificado>"
      },
      "archive": {
        "min_supported_format": 1,
        "max_supported_format": 2,
        "formats": [
          {
            "version": 1,
            "required_members": [
              "dlss5-feed.addon64",
              "dlss5-feed.addon32",
              "DLSS5_Feed.fx",
              "dlss5-feed-host64.exe"
            ]
          },
          {
            "version": 2,
            "required_members": [
              "dlss5-feed.addon64",
              "dlss5-feed.addon32",
              "reshade-shaders/Shaders/DLSS5_Feed.fx",
              "host64/dlss5-feed-host64.exe"
            ]
          }
        ]
      }
    }
  }
}
```

`schema_version` controla o formato do nosso JSON. `min_supported_format` e `max_supported_format` controlam as variantes de layout que o instalador entende. Um layout upstream sem número próprio recebe uma versão interna determinada pelo conjunto de membros reconhecido.

Cada entrada estável deve conter:

- identificador lógico do componente;
- provider e repositório, quando aplicáveis;
- tag, commit ou versão estável imutável;
- asset ID, quando o provider oferecer um;
- nome exato e URL HTTPS;
- SHA-256 esperado;
- tamanho esperado, quando conhecido;
- tipo do artefato;
- arquivos ou padrões obrigatórios;
- formato mínimo e máximo suportados;
- regras específicas de arquitetura, quando existirem.

## Componentes cobertos

| Componente | Descoberta principal | Validação mínima |
| --- | --- | --- |
| DLSS5-Feeder | Latest release do GitHub | addons x86/x64, shader, host64 e layout Vulkan reconhecido |
| RenoDX DLSS5 | Releases do `RankFTW/rhi-repo` | tag compatível e `renodx-dlss5.addon64` |
| Manifesto RHI | Arquivo de snapshot do `RankFTW/RHI` | JSON tipado e entradas NR/SR válidas |
| NVIDIA NGX NR/SR | URLs fornecidas pelo manifesto RHI | HTTPS, SHA fixado no fallback e DLL canônica no ZIP |
| ReShade headers | Snapshot do `crosire/reshade-shaders` | três headers obrigatórios e hashes esperados |
| LumeniteFX | Archive de commit do GitHub | estrutura de shaders/texturas reconhecida |
| dgVoodoo2 | Latest release do GitHub | D3D9 da arquitetura pedida, config e control panel |
| ReShade Addon | Página e download oficiais | versão extraída, executável não vazio e SHA fixado no fallback |

## Modelos e responsabilidades

Adicionar modelos estritamente tipados para o manifesto e evitar dicionários `Any` fora da fronteira de parsing.

- `EmbeddedUpstreamManifest`: carrega e valida o JSON uma vez por execução.
- `ComponentPolicy`: descreve descoberta, fallback e formatos aceitos.
- `PinnedArtifact`: representa URL, revisão, nome, ID, SHA-256 e tamanho esperados.
- `ArchiveFormat`: reconhece e valida um layout de conteúdo.
- `ResolvedArtifact`: resultado neutro de provider, incluindo se veio de latest ou fallback.
- `ResolutionWarning`: código estável, componente, causa, versão latest rejeitada e versão fallback escolhida.
- `ArtifactValidator`: valida URL, tamanho, digest, archive safety, membros e arquitetura.
- `UpstreamResolver`: coordena latest, validação, fallback e promoção atômica ao cache.

O adaptador GitHub continua responsável apenas por traduzir a API do provider para releases, assets, snapshots e arquivos. A política de fallback e a validação permanecem independentes do GitHub, permitindo um futuro adaptador de mirror sem duplicar regras.

## Fluxo de resolução

Para cada componente:

1. Carregar e validar a política embarcada.
2. Consultar latest por meio do adaptador configurado.
3. Selecionar o asset pelo nome quando houver múltiplos candidatos.
4. Quando existir apenas um ZIP, aceitá-lo como candidato e validar seu conteúdo temporariamente.
5. Rejeitar latest por URL inválida, erro definitivo, timeout esgotado, digest divergente, archive inseguro, conteúdo ausente ou formato incompatível.
6. Emitir um warning tipado contendo a razão exata da rejeição.
7. Baixar a versão estável fixada para outro temporário.
8. Conferir SHA-256, tamanho quando disponível, segurança do archive, arquivos obrigatórios e formato.
9. Emitir warning claro com a tag latest rejeitada e a versão estável efetivamente usada.
10. Promover o artefato validado ao cache por substituição atômica e gravar sua proveniência.
11. Se o fallback também falhar, agregar as duas causas e abortar a resolução.

Quando a API GitHub publicar um digest SHA-256 confiável para o asset latest, validá-lo. Sem digest upstream, calcular e registrar o hash observado, mas não tratá-lo como identidade previamente confiável; a compatibilidade ainda depende da validação estrutural.

## Warnings e diagnóstico

Definir códigos estáveis para permitir testes e futura saída estruturada:

- `UPSTREAM_DISCOVERY_FAILED`;
- `UPSTREAM_ASSET_MISSING`;
- `UPSTREAM_AMBIGUOUS_ASSETS`;
- `UPSTREAM_DOWNLOAD_TIMEOUT`;
- `UPSTREAM_HTTP_REJECTED`;
- `UPSTREAM_DIGEST_MISMATCH`;
- `UPSTREAM_ARCHIVE_UNSAFE`;
- `UPSTREAM_CONTENT_MISSING`;
- `UPSTREAM_FORMAT_UNSUPPORTED`;
- `UPSTREAM_STABLE_FALLBACK_USED`;
- `UPSTREAM_FALLBACK_FAILED`.

Cada warning deve informar componente, provider, latest observado, motivo, fallback escolhido e caminho do log. Segredos, tokens e URLs autenticadas nunca aparecem na mensagem. O resumo final da instalação deve listar todos os fallbacks utilizados mesmo quando a instalação terminar com sucesso.

## Cache e atomicidade

- Baixar latest e fallback em temporários diferentes no mesmo filesystem do cache.
- Validar antes da substituição do cache conhecido como bom.
- Manter lock por identidade de componente/revisão durante download, validação e promoção.
- Não manter lock enquanto consulta metadata que não modifica estado, salvo se necessário para deduplicar a operação completa.
- Gravar metadata de cache atomicamente com URL, provider, revisão, asset ID, hash, tamanho, formato reconhecido e origem `latest` ou `stable_fallback`.
- Preservar o cache anterior se refresh, validação ou escrita de metadata falhar.
- Permitir reutilização somente após revalidar metadata e SHA-256 local.

## Ordem transacional do pipeline

Mover `StepFetchUpstream` para antes de `StepCleanPreviousInstall`, ou separar resolução e materialização em duas fases equivalentes.

A sequência desejada é:

1. Validar o alvo sem modificá-lo.
2. Resolver e validar todos os upstreams necessários.
3. Somente depois criar snapshot e remover a instalação anterior.
4. Aplicar os arquivos já validados.
5. Salvar o registro e confirmar a transação.

Adicionar um teste que tenha uma instalação anterior real no diretório temporário, provoque falha de latest e fallback e demonstre byte a byte que nenhum arquivo, INI, registro simulado ou install record foi alterado.

## Estratégia de testes sem Windows local

Todos os casos normais devem usar artefatos sintéticos:

- ZIPs montados em memória com arquivos de conteúdo curto;
- addons, DLLs, shaders e executáveis representados por bytes falsos quando nenhuma análise binária é necessária;
- PE32 e PE32+ mínimos e válidos para os testes que dependem da detecção de arquitetura;
- diretório de jogo temporário;
- adaptadores e respostas HTTP falsos;
- relógio monotônico controlado nos testes de timeout;
- nenhuma chamada externa nos testes unitários ou de integração padrão.

Criar uma integração do pipeline por arquitetura que use um executável PE sintético e todos os bundles falsos, execute instalação e desinstalação e valide os arquivos finais. Essa integração deve rodar na matriz existente de Linux, macOS e Windows.

Manter smoke tests reais separados, opt-in e sem participação no resultado do CI normal. Eles podem validar metadata e arquivos oficiais antes de atualizar o manifesto estável, mas devem ter timeout curto e nunca alterar o cache normal do usuário.

## Matriz obrigatória de testes

Para cada componente listado no manifesto:

- latest compatível sem fallback;
- release com um único ZIP e nome inesperado, mas conteúdo compatível;
- asset esperado ausente;
- múltiplos assets ambíguos;
- 404 com uma única tentativa;
- erro transitório com retries limitados;
- timeout global;
- URL não HTTPS;
- ZIP truncado ou inválido;
- path traversal e colisão após flatten;
- arquivo obrigatório ausente;
- formato abaixo do mínimo e acima do máximo;
- fallback válido com warning correto;
- SHA-256 divergente no fallback;
- latest e fallback inválidos com erro agregado;
- cache conhecido como bom preservado após falha;
- segunda execução idempotente e sem novo download;
- seleção x86/x64 quando aplicável.

Adicionar ainda:

- teste de parsing para manifesto vazio, schema desconhecido, componente ausente e campos extras ou inválidos;
- teste que abre o wheel construído e confirma `dlss5_enabler/upstreams.json`;
- teste de todos os códigos de warning e do resumo final;
- teste de factory para provider desconhecido;
- contrato compartilhado que qualquer futuro adaptador de mirror deverá satisfazer;
- teste completo que prova ausência de mutações no jogo quando a resolução falha.

## Ferramenta de atualização do manifesto

Criar uma ferramenta de manutenção que receba um componente e uma revisão explícita, baixe em diretório temporário, calcule SHA-256 e tamanho, valide o conteúdo e produza a entrada JSON candidata. Ela não deve atualizar o manifesto silenciosamente nem escolher latest sem mostrar a revisão resolvida.

O fluxo de atualização será:

1. Gerar entrada candidata.
2. Exibir revisão, URL, asset ID, tamanho, hash e formato detectado.
3. Atualizar o JSON somente por opção explícita.
4. Rodar todos os testes do componente.
5. Construir o wheel e verificar seu conteúdo.

## Fases de implementação

### Fase 1: manifesto e modelos

- Definir schema versionado.
- Popular pins reais com hashes verificados.
- Implementar loader via recursos do pacote.
- Garantir inclusão no wheel.
- Cobrir parsing, validação e empacotamento.

### Fase 2: validação e resolução

- Implementar validadores reutilizáveis.
- Implementar estado `latest` versus `stable_fallback`.
- Tornar warnings tipados e visíveis.
- Promover downloads ao cache somente depois da validação.

### Fase 3: migrar componentes

- Feeder.
- RenoDX.
- RHI e NGX.
- ReShade headers.
- LumeniteFX.
- dgVoodoo2.
- ReShade Addon oficial.

Cada migração só é concluída quando todos os cenários específicos do componente estiverem cobertos.

### Fase 4: transação e integração sintética

- Resolver tudo antes de limpar a instalação anterior.
- Criar fixtures PE32/PE32+ mínimas.
- Executar pipeline sintético completo para x86 e x64.
- Testar rollback, uninstall e idempotência.

### Fase 5: manutenção e documentação

- Adicionar ferramenta explícita de atualização dos pins.
- Documentar warnings e política de fallback no README.
- Registrar como adicionar um novo provider ou mirror.
- Validar wheel, instalação isolada e matriz CI.

## Critérios de aceite

- `uv run dlss5-enabler check` passa sem erros ou warnings das ferramentas.
- O wheel contém e consegue carregar o manifesto fora do checkout.
- Cada componente tem pin estável, SHA-256 real e validação de conteúdo.
- Todo uso de fallback gera warning específico e aparece no resumo.
- Uma release latest incompatível não interrompe a instalação quando o fallback é válido.
- Latest e fallback inválidos encerram em tempo finito e preservam jogo, instalação anterior e cache válido.
- Os testes padrão não acessam a internet.
- O pipeline sintético passa em Windows, Linux e macOS para x86 e x64.
- Commits e tag da versão resultante são assinados.

## Publicação sugerida

Tratar esta mudança como `v1.1.0`, pois introduz manifesto versionado, política de resolução e comportamento observável novo. Antes da tag:

1. Executar a verificação unificada local.
2. Construir e inspecionar sdist e wheel.
3. Executar manualmente os smoke tests reais opt-in.
4. Publicar a branch e aguardar toda a matriz CI.
5. Criar tag assinada somente depois do CI verde.
6. Acompanhar publicação no PyPI e criação da release GitHub até o estado terminal.
