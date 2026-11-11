# V-GUARD — Integração (visão + gerenciador de eventos)

Junta visão e gerenciador na Raspberry Pi 4, em **dois programas** (Issue #10): o principal
(`vguard.py`, no Python 3.13 do sistema) e a visão (`vision/main.py`, no venv 3.11 com o MediaPipe),
que o principal inicia como processo filho e reinicia se ela cair. Quando a câmera CSI usa outro
Python, a captura roda num auxiliar da visão:

| Pasta     | Origem                                       | Papel                                                        |
|-----------|----------------------------------------------|--------------------------------------------------------------|
| `vision/` | `visao/edge/vguard/vision`                   | Câmera → MediaPipe → EAR/MAR/pose → decide *quando* há evento |
| `events/` | `gerenciador_eventos/edge/vguard/events`     | Grava telemetria e eventos no SD e os envia ao servidor (MQTT e HTTPS) |
| `vguard.py` | novo                                       | O programa principal: cria tudo e cuida da visão             |

O principal não importa o OpenCV, o MediaPipe nem o NumPy: da `vision/` ele só usa o
`options.py` (as opções da câmera) e o `protocol.py` (o canal), os dois só com a biblioteca padrão.

## Como funciona

```
 programa da visão (vision/main.py, venv 3.11)          programa principal (vguard.py, Python 3.13)
 câmera ──► VisionPipeline
              │ detector.py: should_record=True
              │ frame → JPEG em memória
              ▼                            canal (pipe)
            ChannelSink.record_event ═══ evento + JPEG ═══► VisionProcess ──record_event──► EventManager ──► SD
            pronto, fps a cada 10 s ═════════════════════►  (reinicia a visão)               ▲      │
                                                                                            │      │
 ESP32 ──USB───► Esp32Reader ─manager.update("imu"|"obd")────────────────────────────────────┤      │
 GNSS ───NMEA──► GpsReader ───manager.update("gps")──────────────────────────────────────────┘      │
                                                                                                   ▼
                                            MqttPublisher ◄── on_telemetry ──┤ ──► MQTT: telemetria e saúde
                                              EventSender ◄── pending_events ─┘ ──► MQTT: evento + janela
                                                                                     HTTPS: PUT da foto
```

1. **Visão** (outro processo; a janela do OpenCV, quando há, fica nele) lê um frame, extrai as métricas e
   passa ao `DriverStateDetector`. Quando uma condição dura o suficiente (bocejo ≥ 1,5 s ou cabeça desviada
   ≥ 2 s), o detector devolve `should_record=True` **uma vez por episódio**.
2. Nesse frame, a visão codifica a imagem **sem overlays** em JPEG e chama o `record_event` do mensageiro
   (`ChannelSink`), que manda o evento pelo canal ao principal.
3. **VisionProcess** (thread do principal, `events/vision_process.py`) lê o canal, chama o
   `record_event` do **EventManager**, acompanha o `vision_fps` e reinicia a visão se ela cair
   ([a visão num processo filho](#a-visão-num-processo-filho)).
4. **EventManager** anexa o retrato da telemetria (a última amostra de cada sensor, menos as com
   mais de 3 s), grava a foto e depois a linha no SQLite ([o que fica no SD](#o-que-fica-no-sd)).
5. **Esp32Reader** (thread) lê as linhas de texto do ESP32 pela USB (`$A`, `$O`, `$S`...), confere o
   checksum e a `seq`, manda a hora (`$H`) e a taxa do OBD (`$C`) e alimenta `manager.update()` com
   IMU e OBD-II. As amostras só entram depois de 10 linhas válidas seguidas. Ver [o leitor do ESP32](#leitor-do-esp32).
6. **GpsReader** (thread) lê as frases RMC do receptor GNSS e grava `{"lat", "lon", "speed"}` (km/h)
   em `manager.update("gps")`. Sem fix, grava `{}`, para o evento não levar uma posição velha.
7. **MqttPublisher** (`events/mqtt_publisher.py`, Issue #18) publica cada linha de 1/s da telemetria e,
   a cada 60 s, a saúde, com QoS 0, e se reconecta sozinho quando o broker (ou o túnel até ele) volta.
8. **EventSender** (thread, `events/event_sender.py`) manda cada evento quando a janela de ±10 s fecha:
   o evento pelo MQTT (QoS 1) e a foto num `PUT` HTTPS, que confirma o evento. Sem enlace ou com o
   servidor fora, o evento espera no cartão ([o envio ao servidor](#o-envio-ao-servidor)).
9. **PiMonitor** (thread) lê a temperatura e o `throttled` da Pi a cada 10 s e os escreve no log
   ([o que aparece no log](#o-que-aparece-no-log)). Os valores ficam guardados para a saúde.

### A visão num processo filho

O principal cria um pipe, inicia `vision/main.py --channel-fd N` no Python de `VGUARD_VISION_PYTHON`
(padrão: o venv 3.11 da Pi; no notebook, o mesmo Python do principal) e lê as mensagens dele
(`vision/protocol.py`): uma linha de JSON de até 1 KB por mensagem e, no `evento`, os bytes do JPEG
logo depois.

| Mensagem | Quando | Linha |
|---|---|---|
| `pronto` | uma vez, com a câmera aberta e o MediaPipe carregado | `{"msg":"pronto","versao":1}` |
| `fps` | a cada 10 s | `{"msg":"fps","fps":13.9}` |
| `evento` | a cada detecção | `{"msg":"evento","ts":1793980812.437,"type":"fadiga","score":0.82,"jpeg":40074}` + o JPEG |

O stdout e o stderr da visão vão para o mesmo log do principal; o canal só leva as mensagens.
Uma linha acima de 1 KB, JSON inválido, campo faltando, JPEG sem `FF D8` ou acima de 5 MB e a
mensagem cortada no fim são descartados, com um aviso no log.

| Saída da visão | O principal |
|---|---|
| 0, fim normal (o Q na tela ou o `--max-frames`) | encerra o programa todo |
| 78, erro de configuração: calibração pendente sem tela, perfil de outra câmera, resolução diferente da do perfil, modelo ausente, versão errada do canal | não reinicia; a telemetria continua, com o `vision_fps` em 0 |
| outro (inclusive morta por sinal; 143 é o SIGTERM) | reinicia, com a espera de 2 s dobrando até 60 s, que volta a 2 s depois de 60 s rodando |

A primeira queda de um episódio escreve uma linha no log, e as tentativas seguintes não.
No SIGTERM ou Ctrl+C, o principal manda o SIGTERM à visão, espera até 5 s e, se for preciso, a
mata; depois para o resto e fecha o banco. A visão ignora o Ctrl+C (quem cuida dele é o principal).
Sem o principal, a visão roda sozinha e escreve as mensagens no stdout:

```bash
.venv/bin/python vision/main.py --source 0 --headless
```

### O contrato entre os dois lados

É uma chamada só:

```python
event_id = manager.record_event(
    type="fadiga" | "distracao",
    score=0.70..1.0,        # severidade calculada pelo detector
    jpeg=bytes,             # frame original, qualidade 85
    ts=time.time(),         # horário de parede (o detector usa time.monotonic())
)
# -> str (UUID) se gravou; None se caiu no cooldown
```

- **Cooldown:** fica **só** no `EventManager` (`EVENT_COOLDOWN = 5.0` em `events/event_manager.py`).
  A visão não tem cooldown próprio, apenas a trava de episódio do detector. Quando a chamada
  devolve `None`, o principal não conta o evento.
- **No processo filho,** a visão chama o `record_event` do mensageiro (`ChannelSink`), com a mesma
  assinatura; quem chama o do `EventManager` é o `VisionProcess`, no principal.
- O `events/` usa só a biblioteca padrão do Python, menos o `mqtt_publisher.py`, que precisa do `paho-mqtt`
  (`requirements-principal.txt`, no `venv-principal`) e só o importa ao conectar. Toda a dependência pesada
  (OpenCV, MediaPipe) está em `vision/`.

## Executar

Todos os comandos abaixo partem de `integracao/`.

```bash
pip install -r vision/requirements.txt
python3 vision/scripts/download_model.py     # baixa vision/models/face_landmarker.task (se faltar)
```

> **Câmeras:** a padrão é a **câmera noturna OV5647** (CSI, 5 MP, 2 LEDs
> infravermelhos), ver [docs/camera-ov5647.md](docs/camera-ov5647.md). A webcam
> USB **Logitech C525** usa `--source 0` (sem visão noturna), ver
> [docs/camera-c525.md](docs/camera-c525.md).

```bash
python3 vguard.py --headless                 # na Pi, câmera CSI OV5647 via Picamera2, sem janela
```

```bash
python3 vguard.py --source video.mp4         # no notebook, com HUD (q para sair)
```

Outras opções: `--source 0` (webcam USB, ex.: C525) ou `--source /dev/v4l/by-id/...` (a webcam por um nome fixo), `--width`/`--height` (padrão 640×480),
`--fps 30`, `--camera-num 0` e `--infrared`. A fonte padrão é `picamera2` (CSI).
Se o Python da visão não tiver `picamera2` (caso do Raspberry Pi OS Trixie, com a
visão num ambiente 3.11), a captura passa sozinha para o Python do sistema
(`/usr/bin/python3`); `--camera-python` só é preciso para usar outro interpretador.
Para a câmera noturna OV5647, siga [o preparo e os testes na Raspberry Pi](docs/camera-ov5647.md).
Para montagem lateral junto ao retrovisor, use a [calibração guiada do motorista](docs/calibracao-motorista.md).
Olhos são um sinal auxiliar da distração e não disparam fadiga isoladamente.

Os [resultados medidos na Pi 4B](docs/validacao-ov5647.md) incluem captura real
com rosto, pouca luz e iluminação IR.

### Raspberry Pi 4B: ambiente validado e execução local

Na Pi 4B sem AES, utilize Python 3.11 e as versões de `requirements-pi4-py311.txt`.
O ambiente já validado está em `/home/vguard/vguard/compat/mediapipe-0.10.18-py311/.venv`.
Não aplique `vision/requirements.txt` a esse ambiente: MediaPipe 0.10.18 exige NumPy <2.
Não instale simultaneamente `opencv-python` e `opencv-contrib-python`.

O MediaPipe 0.10.18 é a última versão da série 0.10 com pacote para Linux ARM 64 bits, e só
para o Python 3.9 a 3.12; o Debian 13 traz o 3.13. Por isso a visão roda num venv 3.11, montado
pelo [`uv`](https://docs.astral.sh/uv/) sem sudo (validado em 08/11/2026, Pi 4B Rev 1.5):

```bash
# 1. uv em ~/.local/bin (o ~/.profile do Debian já põe essa pasta no PATH)
B=https://github.com/astral-sh/uv/releases/download/0.12.21
curl -fsSLO $B/uv-aarch64-unknown-linux-gnu.tar.gz
curl -fsSLO $B/uv-aarch64-unknown-linux-gnu.tar.gz.sha256
sha256sum -c uv-aarch64-unknown-linux-gnu.tar.gz.sha256
tar xzf uv-aarch64-unknown-linux-gnu.tar.gz
install -D -m 755 -t ~/.local/bin uv-aarch64-unknown-linux-gnu/uv uv-aarch64-unknown-linux-gnu/uvx

# 2. Python 3.11 e o venv, só com pacotes prontos (nada é compilado na Pi)
V=/home/vguard/vguard/compat/mediapipe-0.10.18-py311/.venv
uv python install 3.11.16
uv venv --python 3.11.16 $V
cd /home/vguard/vguard/integracao
uv pip install --python $V/bin/python --only-binary :all: -r requirements-pi4-py311.txt

# 3. O link que o tools/test_camera_gui.sh usa, e o modelo (fica fora do git)
ln -s $V .venv
.venv/bin/python vision/scripts/download_model.py
```

O `requirements-pi4-py311.txt` tem a lista completa do venv que funcionou na Pi (`uv pip freeze`).
Na Pi 4B, o `grep Features /proc/cpuinfo` mostra `fp asimd evtstrm crc32 cpuid`, sem `aes`.

O programa principal roda no Python 3.13 do sistema, num venv próprio, com o `paho-mqtt` (Issue #18).
Os testes do MQTT precisam também do broker `mosquitto`, com o serviço desligado (o teste o inicia
numa porta própria):

```bash
python3 -m venv /home/vguard/vguard/venv-principal
/home/vguard/vguard/venv-principal/bin/pip install -r requirements-principal.txt
sudo apt install -y mosquitto && sudo systemctl disable --now mosquitto
```

Com o modelo em `vision/models/face_landmarker.task`, execute:

```bash
cd /home/vguard/vguard/integracao
VGUARD_DATA=/home/vguard/vguard/dados-integracao \
  /home/vguard/vguard/venv-principal/bin/python -B \
  vguard.py --headless --source picamera2 --no-esp32 --no-sync
```

A visão roda no venv 3.11 (`VGUARD_VISION_PYTHON`, padrão
`/home/vguard/vguard/compat/mediapipe-0.10.18-py311/.venv/bin/python`). Para a CSI, o Python da
visão precisa importar `picamera2` e `libcamera`; no Debian 13, a captura vai sozinha para um
auxiliar no Python do sistema (`/usr/bin/python3`, ou `--camera-python`), que envia os frames
BGR à visão. `--source 0` continua selecionando uma webcam USB.

`--no-sync` não inicia o MQTT nem o envio das fotos: tudo fica no cartão. `--no-esp32` não inicia o leitor do ESP32
(`--no-uart`, o nome antigo, continua valendo).
Para uma execução limitada, acrescente `--max-frames 120`. Ctrl+C ou SIGTERM encerram
os componentes antes de fechar o banco. Se houver um envio em andamento, o encerramento pode
esperar o timeout da conexão.

Sem o ESP32 ou o GPS conectados, o leitor correspondente registra um aviso e tenta de novo a
cada 2 s. A visão e a gravação de eventos continuam funcionando. Sem a câmera, a visão cai e é
reiniciada com a espera crescente, e a telemetria continua.

### O que aparece no log

O `vguard.py` escreve no log (o stderr) a partir do nível INFO. A cada 10 s, também com `--headless`:

```
2026-11-08 17:31:31,076 INFO pi: Pi: 58.4 °C, throttled 0x0
2026-11-08 17:31:31,130 INFO vision: visão: 13.9 FPS em 10 s
```

- **`visão`:** a média de quadros processados por segundo nos últimos 10 s (`vision/fps.py`).
- **`Pi`** (`events/pi_monitor.py`): o `vcgencmd measure_temp` e o `vcgencmd get_throttled`.
  Sem o `vcgencmd`, a temperatura vem de `/sys/class/thermal/thermal_zone0/temp` e o `throttled`
  fica sem valor, com um aviso uma vez só. Avisos:
  - acima de 75 °C, um por episódio, que termina quando a temperatura volta abaixo de 75 °C
    (a Pi 4 reduz o clock a partir de 80 °C);
  - uma linha por mudança dos bits de "agora" do `get_throttled`: `undervoltage` (subtensão),
    `freq_capped` (frequência limitada), `throttling` (redução de clock) e `soft_temp_limit`
    (limite de temperatura). Os bits 16 a 19, "desde o boot", só aparecem no valor da linha de 10 s.
- **`vision.process`** (o principal cuidando da visão):

```
INFO vision.process: visão iniciada (pid 24095)
INFO vision.process: [EVENTO REGISTRADO] ID=… Tipo='fadiga' Score=0.7 Tamanho=40255B
WARNING vision.process: visão caiu (código -9); nova tentativa em 2 s
INFO vision.process: visão de volta depois de 1 tentativa (6 s fora)
ERROR vision.process: visão: erro de configuração (código 78): sem reinício; a telemetria continua
```

- **`events.mqtt`** (o publicador) e **`events.sender`** (o envio dos eventos):

```
INFO events.mqtt: MQTT conectado (127.0.0.1:18883, vguard/bancada-01/telemetria)
WARNING events.mqtt: MQTT caiu (Unspecified error); reconectando
WARNING events.mqtt: MQTT de volta depois de 42 s
INFO events.sender: evento 12 enviado (fadiga, janela 21 s / 6.9 KB, foto 46 KB, 3.1 s)
WARNING events.sender: evento 12: o servidor não gravou o evento (409 x3); publicado de novo na próxima rodada
WARNING events.sender: envio de eventos falhou (HTTP 503); nova tentativa em 60 s
INFO events.sender: envio de eventos de volta depois de 3 tentativas
WARNING events.sender: servidor recusou a foto do evento 12 (HTTP 400): fica no cartão
ERROR events.sender: token recusado (HTTP 401): confira o VGUARD_TOKEN_FILE; nova tentativa em 60 s
INFO events.sender: envio de eventos: MQTT desconectado; 2 eventos esperam no cartão
```

- **Ao encerrar** (SIGTERM, Ctrl+C ou o fim normal da visão), uma linha de resumo. A média é a
  dos `fps` da visão. A parte do ESP32 só aparece com ele ligado, e a do envio, sem o `--no-sync`:

```
INFO vguard: resumo: visão 13.8 FPS em média; eventos: fadiga 1, distracao 1; visão reiniciada 0 vezes; ESP32: lost 0, corrupt 0; mqtt: 594 linhas publicadas, 0 não publicadas (desconectado), 10 saúdes; envio: 2 eventos enviados, 0 pendentes; Pi até 61.2 °C, throttled sem avisos
```

### Bancada guiada da visão (passo 0 e passo C)

Na tela da Pi (Raspberry Pi Connect), depois da calibração:

```bash
cd ~/vguard/integracao
.venv/bin/python tools/vision_bench.py             # passo 0: só a visão; pasta ../dados-integracao
.venv/bin/python tools/vision_bench.py --passo-c   # passo C: com o ESP32 (/dev/ttyUSB0) e o kill -9
```

A visão roda sem tela, como no critério. Uma janela à parte mostra em letra grande o que quem lê
a tela deve pedir, em voz alta, a quem está na frente da câmera; quem está na câmera olha sempre
para a frente, nunca para a tela. A sequência começa no **ESPAÇO** e acaba no **N**, depois do
último pedido (ou sozinha, nos 10 min do `--seconds`):

1. `OLHE PARA A FRENTE` por 2 min, com a contagem até o próximo pedido;
2. `PREPARE-SE` por 5 s, e então `PECA PARA BOCEJAR`, até aparecer `OK! PODE FECHAR A BOCA`
   (o evento `fadiga` chegou ao log). Depois de 20 s sem o evento, a tela pede mais; em 60 s,
   `NAO DETECTOU`, e a sequência segue;
3. mais 2 min olhando para a frente, e `PECA PARA VIRAR O ROSTO`, do mesmo jeito (`distracao`);
4. `PEDIDOS FEITOS`: o **N** termina a bancada, e o fim mostra os critérios.

**N** adianta o passo, pula o pedido atual ou, na última parte, termina a bancada. **Q** interrompe,
com o fim `CONFERIR` mesmo com os critérios cumpridos. Um evento fora de um pedido
aparece em vermelho. O log inteiro vai para o terminal e para
`../dados-integracao/bancada-visao-<data>.log`, e o fim mostra cada critério com `OK` ou `FALHOU`.

No **passo C** (`--passo-c`, ou `--esp32 [PORTA]` e `--kill-vision`), o principal lê também o ESP32,
e depois do rosto virado vem `VISAO MORTA (KILL -9)`: a ferramenta mata a visão e espera
`OK! A VISAO VOLTOU`. No fim, ela confere no SQLite a telemetria do tempo que rodou: ~1 linha por segundo,
cada uma com `imu` e `obd`, nenhuma lacuna acima de 1,5 s (nem no kill -9), o maior `age_ms` abaixo
de 1 s e o `lost`/`corrupt` do ESP32 em 0. O principal roda no Python dele (`VGUARD_PYTHON`, o
`venv-principal` ou o atual).

Para guardar o log de uma execução com `timeout`, redirecione para um arquivo
(`> bancada.log 2>&1`). Sem o `--foreground`, o `timeout` manda o sinal ao grupo de processos
inteiro, e um `| tee` morreria junto, antes de escrever o resumo.

### Variáveis de ambiente

| Variável         | Padrão                       | Uso                                  |
|------------------|------------------------------|--------------------------------------|
| `VGUARD_DATA`    | `dados`                      | Pasta no cartão SD                    |
| `VGUARD_ESP32`   | `/dev/vguard-esp32`          | USB do ESP32 (no notebook, `/dev/ttyUSB0`) |
| `VGUARD_OBD_HZ`  | `12.5`                       | Taxa do OBD pedida ao ESP32 pelo `$C` (1 a 20, até 1 casa) |
| `VGUARD_GPS`     | `/dev/serial0`               | UART ligada ao receptor GNSS          |
| `VGUARD_VISION_PYTHON` | o venv 3.11 da Pi; sem ele, o Python do principal | O Python do programa da visão |
| `VGUARD_PYTHON`  | o `venv-principal`; sem ele, o `.venv` ou o atual | O Python do principal nas ferramentas (`tools/`) |
| `VGUARD_GPS_BAUD`| `9600`                       | Velocidade da UART do GNSS            |
| `VGUARD_BACKEND` | `https://vguard.example/api` | URL do backend; as fotos vão a `<URL>/events/<event_id>/photo` |
| `VGUARD_DEVICE`  | `vguard-01`                  | O `device_id`: o meio dos tópicos MQTT e o `X-Device-Id` |
| `VGUARD_TOKEN_FILE` | vazio                     | Arquivo com o token do backend (`Authorization: Bearer`); na bancada, `~/vguard-mqtt/token` |
| `VGUARD_TOKEN`   | vazio                        | O token, se não houver o `VGUARD_TOKEN_FILE` |
| `VGUARD_WIFI`    | vazio                        | Redes Wi-Fi (nomes do NetworkManager, por vírgula) que podem levar as fotos; sem nenhuma, só o 4G |
| `VGUARD_MQTT_HOST` / `VGUARD_MQTT_PORT` | `127.0.0.1` / `1883` | O broker; na bancada, o túnel SSH em `18883` |
| `VGUARD_MQTT_CA` | vazio (sem TLS)              | A CA do TLS; na bancada, `~/vguard-mqtt/ca.crt` |
| `VGUARD_MQTT_USER` | o `VGUARD_DEVICE`          | O usuário do MQTT; na bancada, `pi-bancada-01` |
| `VGUARD_MQTT_PASSWORD_FILE` | vazio             | Arquivo com a senha do MQTT; na bancada, `~/vguard-mqtt/senha` |
Os limiares da visão (EAR, MAR, yaw, pitch e tempos) ficam em `vision/config.py`.

## Leitor do ESP32

O ESP32 (repositório `firmware_esp32`) fica na USB da Pi e manda linhas de texto
`$<tipo>,<seq>,<t>,...*<hh>` a 115200 baud. O `events/esp32_reader.py`:

- confere cada linha: até 120 caracteres, checksum (o XOR do NMEA) e a `seq`, que soma 1 por linha;
- conta as perdas: cada lacuna na `seq` soma em `lost`, e cada linha descartada em `corrupt`.
  O texto do boot do ESP32 e o que chega nos primeiros 0,2 s depois de abrir a porta não contam;
- manda o `$H` (a hora da Pi, só depois de sincronizada) ao ver um `$B` ou um `$S` com `ESPERA`,
  e a cada 60 s; e o `$C,obd_hz,<n>` quando o `$S` mostra uma taxa diferente da `VGUARD_OBD_HZ`;
- aceita as amostras depois de 10 linhas válidas seguidas: `$A` vira `update("imu", ...)` e `$O`
  vira `update("obd", {..., "age_ms": ...})`;
- marca o sensor sem dado (só o horário) quando o OBD não está em `LENDO`, quando o `$A` some e
  quando o ESP32 desaparece por 3 s.

Na bancada, com o firmware `esp32-sim` gravado no ESP32 e a placa na USB do notebook:

```bash
python3 tools/esp32_bench.py --port /dev/ttyUSB0 --seconds 600      # 10 min, resumo no fim
python3 tools/esp32_bench.py --port /dev/ttyUSB0 --seconds 30 --obd-hz 5
python3 tools/esp32_bench.py --port /dev/ttyUSB0 --seconds 20 --no-clock   # sem $H
```

## O envio ao servidor

Pelo contrato 2.1 (Issue #18), todos os dados vão pelo MQTT, e só a foto vai pelo HTTPS:

| O quê | Por onde | Quando |
|---|---|---|
| A telemetria | `vguard/<device>/telemetria`, QoS 0 | cada linha de 1/s, no segundo cheio (`ts` = 11.0, 12.0…) |
| A saúde | `vguard/<device>/saude`, QoS 0 | a cada 60 s e logo depois de cada conexão |
| O evento, com a janela de ±10 s | `vguard/<device>/eventos`, QoS 1 | quando a janela fecha, a partir de `ts + 12 s` |
| A foto | `PUT <VGUARD_BACKEND>/events/<event_id>/photo`, o JPEG cru | 2 s depois do PUBACK do evento |

- **O publicador** (`events/mqtt_publisher.py`) se reconecta sozinho, de 1 s dobrando até 30 s. Uma
  linha de telemetria com o MQTT desconectado não é guardada para depois (QoS 0): fica pendente no cartão.
- **O `event_id`** é `uuid5(NAMESPACE_URL, "vguard:<device>:<ts em ms>:<type>")`: o mesmo evento dá sempre
  o mesmo, e o servidor não duplica um reenvio.
- **A janela** sai das linhas do SQLite dos segundos de `ts − 10` a `ts + 10`: uma entrada `{ts, source, data}`
  por sensor e por linha, sem o `age_ms`. Acima de 15 KiB, ela perde as pontas, com um aviso.
- **O 200 da foto confirma o evento:** o backend só aceita a foto com o evento já gravado.

| Resposta do `PUT` | O evento | Depois |
|---|---|---|
| 200 | confirmado: sai da fila | a espera volta a 60 s |
| 409 (o evento ainda não está no banco) | pendente | novo `PUT` em 30 s; depois de 3, espera crescente e o evento é publicado de novo |
| 400 ou 413 | recusado (`FAILED`): sai da fila e fica no cartão | segue para o próximo |
| 401 | pendente (o token está errado) | a espera crescente |
| 5xx, sem resposta em 30 s, erro de rede ou sem PUBACK | pendente | 60 s, dobrando até 10 min |

- **O enlace** é o do `sync.py` (`active_link()`): o Wi-Fi autorizado (`VGUARD_WIFI`) ou o 4G do modem no
  modo placa de rede (`usb0`). Sem enlace ou com o MQTT desconectado, o evento espera no cartão.
- **A foto apagada do cartão** à mão vira `FAILED`, para não travar a fila.
- **O `--no-sync`** desliga o MQTT e o envio.

**Servidor falso** (`events/fake_server.py`), para os testes e a bancada: o backend (os dois `PUT`, com o
409 sem o evento) e, com o `--mqtt`, também o consumidor, que assina os três tópicos:

```bash
/usr/sbin/mosquitto -p 1883 &
/home/vguard/vguard/venv-principal/bin/python events/fake_server.py --mqtt 127.0.0.1:1883 \
  --dir /home/vguard/vguard/servidor-falso                                  # http://127.0.0.1:8000/api
.venv/bin/python tools/vision_bench.py --passo-c --server                   # noutro terminal
```

Com o `--server`, a bancada guiada roda o principal sem o `--no-sync`, espera a janela do último evento
fechar e confere no fim que toda a telemetria foi publicada e que nenhum evento ficou pendente.

**Os ensaios com o servidor de verdade** (`tools/server_bench.py`): o leitor do ESP32, o publicador e o
envio, de verdade, e eventos sintéticos com a foto `tools/foto-sintetica.jpg` (640×480). No fim, o
relatório para o servidor: os `ts` publicados e as lacunas, as quedas e as voltas do MQTT, e cada evento
com o `event_id` e os status do `PUT`.

```bash
VGUARD_DEVICE=bancada-01 VGUARD_MQTT_PORT=18883 VGUARD_MQTT_CA=~/vguard-mqtt/ca.crt \
VGUARD_MQTT_USER=pi-bancada-01 VGUARD_MQTT_PASSWORD_FILE=~/vguard-mqtt/senha \
VGUARD_BACKEND=https://vguard-projeto.mytunnel.org/api VGUARD_TOKEN_FILE=~/vguard-mqtt/token \
VGUARD_WIFI=<a rede da bancada> \
  /home/vguard/vguard/venv-principal/bin/python tools/server_bench.py --port /dev/ttyUSB0 \
  --seconds 300 --evento-a 60 --data-dir /home/vguard/vguard/dados-ensaio-1
```

## O que fica no SD

```
dados/
├── vguard.db                       SQLite: telemetry (1 linha por segundo) e events
└── photos/<ts em ms>-<tipo>.jpg    a foto de cada evento, como 1793980812437-fadiga.jpg
```

A linha de telemetria do segundo N tem `ts` = N e a última leitura de cada sensor com `ts` em [N, N+1).
Cada evento tem (contrato 2.1, Issue #18):

- `id`: o id local, inteiro;
- `ts`: o instante do quadro, ao milissegundo. O `event_id` do servidor sai de `(device_id, ts, type)`;
- `type` (`fadiga` ou `distracao`) e `score`;
- `telemetria`: o retrato, com a última amostra de cada sensor e o `ts` dela, menos as com mais de 3 s.
  Pode ser `{}`;
- `photo`: o nome da foto, gravada antes da linha no banco;
- `synced`: um estado de envio só: o 200 da foto confirma o evento e a foto juntos.

**Banco do modelo antigo** (UUID e janela de 10 s, até 09/11). Na primeira abertura, ele vai para o lado
com as fotos, sem converter e sem apagar nada: `vguard-antigo.db` e `photos-antigo/`. Se esses nomes já
existirem, os novos levam a data e a hora (`vguard-antigo-20261110-010533.db`). Uma linha de aviso no log
diz quantos eventos e fotos foram:

```text
WARNING events: banco do modelo antigo movido para vguard-antigo.db e photos-antigo/ (412 eventos, 398 fotos); nada foi apagado
```

## Testes

Cada arquivo de teste roda sozinho. A partir de `integracao/`:

```bash
(cd vision && python3 test_metrics.py && python3 test_detector.py && python3 test_pipeline_integration.py && python3 test_fps.py)
python3 -B vision/test_capture.py
python3 -B vision/test_calibration.py
(cd vision && python3 test_protocol.py && python3 test_program.py)
```

```bash
(cd events && python3 test_event_manager.py && python3 test_event_sender.py && python3 test_sync.py && python3 test_esp32_reader.py && python3 test_gps_reader.py && python3 test_pi_monitor.py && python3 test_vision_process.py)
(cd events && /home/vguard/vguard/venv-principal/bin/python test_mqtt_publisher.py)     # o MQTT: paho e mosquitto
python3 -B test_vguard.py
(cd tools && python3 test_vision_bench.py)
```

Se já estiver dentro de `vision/` ou `events/`, rode só os `python3 test_*.py`.

`test_pipeline_integration.py` usa um mock do `EventManager` com a mesma assinatura do real.
`events/test_vision_process.py` usa uma visão falsa (`events/fake_vision.py`) que fala o protocolo do canal;
`vision/test_program.py` roda o programa da visão de verdade com um vídeo sintético (precisa do venv da visão).
O `test_vguard.py` confere que o principal não importa o OpenCV, o MediaPipe nem o NumPy.
`events/test_esp32_reader.py` e `events/test_gps_reader.py` usam um ESP32 e um receptor falsos num pty.
`events/test_mqtt_publisher.py` inicia um `mosquitto` de verdade numa porta livre; sem o `paho-mqtt` ou o
`mosquitto`, os testes dele são pulados, com o motivo. O `test_event_sender.py` usa um publicador falso.
Os testes de `events/` precisam de Linux (na Pi ou no WSL).

Teste opt-in da aplicação com câmera CSI e persistência reais, sem UART/backend,
executado na Raspberry Pi:

```bash
python3 -B tools/validate_camera_persistence.py --source picamera2 \
  --data-dir /caminho/novo-exclusivo-do-teste
```

O teste roda a visão num processo só (o `VisionPipeline` e o `EventManager` reais) e injeta
métricas sintéticas no detector temporal.
O evento recebe `type=teste_integracao_sintetico_fadiga`; o JPEG é do frame real.
Um manifesto identifica os dados de teste. O diretório precisa ser novo para não misturar
esses eventos com registros reais. Na Pi, use o interpretador 3.11 validado acima.

## Pendências

- **GPS (RF02):** o `GpsReader` está testado com um receptor falso. Falta conferir com o módulo
  real a porta, a velocidade (`VGUARD_GPS`, `VGUARD_GPS_BAUD`) e se ele envia RMC. O PPS e o
  acerto do relógio da Pi (gpsd/chrony) ficam fora do leitor; se o gpsd ocupar a porta, o leitor
  passa a ler do gpsd.
- **Responsabilidade:** adaptações para execução conjunta são mantidas neste repositório;
  não há sincronização automática com `visao` ou `gerenciador_eventos`. Os originais são preservados.
