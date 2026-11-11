# integracao (V-GUARD)

Software da Raspberry Pi do V-GUARD: a visão (fadiga e distração pela câmera), o leitor do ESP32 pela USB, o GPS, a gravação local no cartão e o envio ao servidor.

## Onde está a verdade

A especificação e as decisões estão na `proposta-de-desenvolvimento.md`, e os campos enviados ao servidor no `contrato-pi-servidor.md`. Os dois ficam fora deste repositório, na pasta acima do clone (`../`). Quem não tiver os documentos pede ao Thiago. Se algo aqui discordar deles, vale a proposta.

Seções principais: §5 (serial ESP32 ↔ Pi), §6 (Raspberry Pi), §6.8 (o que muda neste repositório e a ordem dos tickets), §8 (servidor) e §9 (roteiro de testes).

## Regras do código

- Nomes em inglês, como no resto do repositório (`update()`, `record_event()`, `EventManager`). Comentários e docstrings em português.
- O código precisa rodar no Python 3.11, o da visão com o MediaPipe 0.10.18 (README), e no 3.13 do sistema da Pi, o do programa principal. O `vguard.py`, o `events/`, o `vision/options.py` e o `vision/protocol.py` usam só a biblioteca padrão: o principal não importa o OpenCV, o MediaPipe nem o NumPy, e o `test_vguard.py` confere isso. A exceção é o `paho-mqtt` (`requirements-principal.txt`, só no `venv-principal`): o `events/mqtt_publisher.py` e o `fake_server.py --mqtt` só o importam ao conectar, para o resto continuar rodando no 3.11.
- Cada arquivo de teste roda sozinho, a partir da própria pasta: `cd events && python3 test_esp32_reader.py`. Os testes da `vision/` precisam da visão instalada (OpenCV e MediaPipe), menos o `test_protocol.py`. O `test_vguard.py`, os do `events/` e o `tools/test_vision_bench.py` rodam também no 3.13 do sistema. O `events/test_mqtt_publisher.py` precisa do `paho-mqtt` e do `mosquitto` (na Pi, no `venv-principal`); sem eles, os testes são pulados.

## Fluxo por ticket

1. Rode `git fetch` e confira o `git status`. Crie um ramo novo a partir do `origin/main`.
2. Implemente com os testes.
3. Cada mensagem de commit cita o ticket (`#N`).
4. Abra o PR com `Closes #N`. O Thiago revisa e faz o merge.
5. Para atualizar um ramo de PR, traga o `origin/main` por `git merge`. Não reescreva o histórico com push forçado.

## Agent skills

### Issue tracker

Issues do GitHub de `V-guard-org/integracao`, pelo `gh`. Veja `docs/agents/issue-tracker.md`.

### Triage labels

Os cinco rótulos padrão: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human` e `wontfix`. Veja `docs/agents/triage-labels.md`.

### Domain docs

Um contexto só. As decisões ficam na proposta, sem `GLOSSARY.md` nem `docs/adr/`. Veja `docs/agents/domain.md`.
