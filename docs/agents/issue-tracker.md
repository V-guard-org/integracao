# Rastreador de issues: GitHub

Os tickets deste repositório ficam nas Issues do GitHub de `V-guard-org/integracao`. Use o `gh` para tudo. Dentro do clone, o `gh` descobre o repositório sozinho pelo `git remote -v`.

## Corpo dos tickets

A proposta (`../proposta-de-desenvolvimento.md`) não está no repositório, e quem lê a issue pode não tê-la. Por isso cada ticket traz os valores concretos de que precisa: formato das linhas, campos do contrato, limites, tempos. A seção da proposta entra só como origem, por exemplo "(proposta §5.5)".

## Comandos

- **Criar uma issue:** `gh issue create --title "..." --body "..."`. Para corpo com várias linhas, use heredoc.
- **Ler uma issue:** `gh issue view <número> --json number,title,body,labels,comments`.
- **Listar issues:** `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`, com os filtros `--label` e `--state` que couberem.
- **Fazer uma issue virar sub-issue de outra:** `gh issue create --parent <pai> ...`, ou depois `gh issue edit <pai> --add-sub-issue <filha>` (`gh` 2.94 ou mais novo). Sem sub-issues, ponha `Parte de #<pai>` no topo do corpo da filha.
- **Comentar:** `gh issue comment <número> --body "..."`.
- **Pôr ou tirar rótulo:** `gh issue edit <número> --add-label "..."` ou `--remove-label "..."`.
- **Fechar:** `gh issue close <número> --comment "..."`.

## Dependências (um ticket bloqueia outro)

Use as dependências nativas do GitHub, que aparecem na tela da issue:

- **Criar a ligação:** `gh api --method POST repos/V-guard-org/integracao/issues/<bloqueado>/dependencies/blocked_by -F issue_id=<id-do-bloqueador>`.
- **Achar o id:** o `<id-do-bloqueador>` é o id numérico de banco da issue, `gh api repos/V-guard-org/integracao/issues/<n> --jq .id`. Não é o `#número` nem o `node_id`.
- **Ver os bloqueios:** o GitHub informa em `issue_dependencies_summary.blocked_by` quantos bloqueadores seguem abertos.
- **Sem dependências nativas:** ponha uma linha `Bloqueado por: #<n>, #<n>` no topo do corpo.

Um ticket está livre quando todos os bloqueadores estão fechados.

## PRs como pedidos de mudança

**PRs como pedidos de mudança: não.** _(Mude para `sim` se PRs de fora passarem a contar como pedidos de funcionalidade; o `/triage` lê esta opção.)_

Com `sim`, os PRs passam pelos mesmos rótulos e estados das issues, com os equivalentes `gh pr`:

- **Ler um PR:** `gh pr view <número> --comments`, e `gh pr diff <número>` para ver a mudança.
- **Listar PRs de fora para triagem:** `gh api --paginate 'repos/{owner}/{repo}/pulls?state=open' --jq '.[] | select(.author_association | IN("OWNER","MEMBER","COLLABORATOR") | not) | {number, title, author: .user.login, author_association, labels: [.labels[].name]}'`.
- **Comentar, rotular ou fechar:** `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

Issues e PRs dividem a mesma numeração no GitHub, então um `#42` pode ser qualquer um dos dois. Tente `gh pr view 42` e, se falhar, `gh issue view 42`.

## Quando uma skill diz "publicar no rastreador"

Crie uma issue no GitHub.

## Quando uma skill diz "buscar o ticket"

Leia como em **Ler uma issue**, acima.

## Operações do `/wayfinder`

O **mapa** é uma issue só, com os tickets como **filhas**.

- **Mapa:** uma issue com o rótulo `wayfinder:map`, com o corpo em Notas / Decisões até agora / Névoa. `gh issue create --label wayfinder:map`.
- **Ticket filho:** issue ligada ao mapa como sub-issue (veja **Fazer uma issue virar sub-issue**). Rótulos `wayfinder:<tipo>` (`research`, `prototype`, `grilling` ou `task`). Quem pega o ticket é marcado como responsável.
- **Bloqueio:** as dependências nativas, como em **Dependências**, acima.
- **Fronteira:** as filhas abertas do mapa sem bloqueador aberto e sem responsável; vale a primeira na ordem do mapa.
- **Pegar:** `gh issue edit <n> --add-assignee @me`, a primeira escrita da sessão.
- **Resolver:** `gh issue comment <n> --body "<resposta>"`, depois `gh issue close <n>`, depois um resumo com link no "Decisões até agora" do mapa.
