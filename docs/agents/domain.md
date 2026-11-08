# Documentos de domínio

Como as skills de engenharia leem as decisões deste repositório.

## Antes de explorar, leia

- **`../proposta-de-desenvolvimento.md`**, fora do repositório: é a especificação e o registro das decisões do V-GUARD. Para este repositório, importam mais a §5 (serial ESP32 ↔ Pi), a §6 (Raspberry Pi), a §6.8 (o que muda aqui e a ordem dos tickets), a §8 (servidor) e a §9 (roteiro de testes).
- **`../contrato-pi-servidor.md`**, também fora do repositório: os campos exatos que a Pi envia ao servidor (MQTT e `POST /api/events`).

A proposta é longa, então leia por seção: `grep -n '^#' ../proposta-de-desenvolvimento.md` dá a linha de cada título, e o `Read` com `offset` e `limit` traz só a seção que o trabalho pede.

Se a proposta não estiver acessível, como num clone de outra pessoa, siga o ticket. Cada ticket traz os valores concretos de que precisa.

## Sem glossário nem ADRs

Este repositório não tem `GLOSSARY.md`, `GLOSSARY-MAP.md` nem `docs/adr/`, e não deve ganhar esses arquivos: a proposta faz esse papel. Quando uma skill, como `/domain-modeling` ou `/grill-with-docs`, quiser registrar um termo ou uma decisão, registre na proposta. Siga as regras do `CLAUDE.md` da pasta `Oficinas2/`:

- status Decidido, Proposto ou Em aberto;
- lista de itens em aberto da §10;
- linha datada no registro de alterações;
- campo enviado ao servidor que mudou: atualize também o contrato e a versão dele.

## Use o vocabulário da proposta

Ao nomear um conceito (título de ticket, nome de teste, proposta de refatoração), use o termo da proposta e do contrato, como `ATIVO`, `ESPERA`, `$A`, `$S`, `seq`, `esp32.lost` e `age_ms`. Não invente sinônimos. Os nomes do código ficam em inglês, como no resto do repositório, e os comentários e docstrings em português.

Se o conceito ainda não tem nome na proposta, é um sinal: ou o termo é inventado (reconsidere), ou falta uma decisão (aponte para o Thiago).

## Aponte conflitos com a proposta

Se o trabalho contradiz a proposta ou o contrato, diga isso claramente em vez de passar por cima:

> _Contradiz a §5.5 (10 linhas válidas antes das amostras), mas vale reabrir porque…_

Quem decide é o Thiago. Até ele confirmar, vale a proposta.
