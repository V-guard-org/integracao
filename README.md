# V-GUARD — Integração (visão + gerenciador de eventos)

Junta visão e gerenciador na Raspberry Pi 4, em **dois programas** (Issue #10): o principal
(`vguard.py`, no Python 3.13 do sistema) e a visão (`vision/main.py`, no venv 3.11 com o MediaPipe),
que o principal inicia como processo filho e reinicia se ela cair. Quando a câmera CSI usa outro
Python, a captura roda num auxiliar da visão:

| Pasta     | Origem                                       | Papel                                                        |
|-----------|----------------------------------------------|--------------------------------------------------------------|
| `vision/` | `visao/edge/vguard/vision`                   | Câmera → MediaPipe → EAR/MAR/pose → decide *quando* há evento |
| `events/` | `gerenciador_eventos/edge/vguard/events`     | Grava telemetria e eventos no SD e sincroniza com o backend  |
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
                                                                    Sync ◄── pending_* / ack_* ──► backend
```

1. **Visão** (outro processo; a janela do OpenCV, quando há, fica nele) lê um frame, extrai as métricas e
   passa ao `DriverStateDetector`. Quando uma condição dura o suficiente (bocejo ≥ 1,5 s ou cabeça desviada
   ≥ 2 s), o detector devolve `should_record=True` **uma vez por episódio**.
2. Nesse frame, a visão codifica a imagem **sem overlays** em JPEG e chama o `record_event` do mensageiro
   (`ChannelSink`), que manda o evento pelo canal ao principal.
3. **VisionProcess** (thread do principal, `events/vision_process.py`) lê o canal, chama o
   `record_event` do **EventManager**, acompanha o `vision_fps` e reinicia a visão se ela cair
   ([a visão num processo filho](#a-visão-num-processo-filho)).
4. **EventManager** anexa a última amostra de cada sensor, grava a foto e a linha no SQLite
   e guarda a janela de telemetria de 10 s antes e 10 s depois do evento (RF17).
5. **Esp32Reader** (thread) lê as linhas de texto do ESP32 pela USB (`$A`, `$O`, `$S`...), confere o
   checksum e a `seq`, manda a hora (`$H`) e a taxa do OBD (`$C`) e alimenta `manager.update()` com
   IMU e OBD-II. As amostras só entram depois de 10 linhas válidas seguidas. Ver [o leitor do ESP32](#leitor-do-esp32).
6. **GpsReader** (thread) lê as frases RMC do receptor GNSS e grava `{"lat", "lon", "speed"}` (km/h)
   em `manager.update("gps")`. Sem fix, grava `{}`, para o evento não levar uma posição velha.
7. **Sync** (thread) envia a cada 60 s o que está pendente: metadados, telemetria e fotos por 4G ou Wi-Fi
   (as fotos vão por último e, se falharem, atrasam só a si mesmas). Um item só é marcado como enviado depois do 2xx do backend (RF22).
8. **PiMonitor** (thread) lê a temperatura e o `throttled` da Pi a cada 10 s e os escreve no log
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
- O `events/` usa só a biblioteca padrão do Python. Toda a dependência pesada (OpenCV, MediaPipe) está em `vision/`.

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

O programa principal roda no Python 3.13 do sistema, num venv próprio (por enquanto sem pacote
nenhum; o MQTT entra no ticket 4):

```bash
python3 -m venv /home/vguard/vguard/venv-principal
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

`--no-sync` não inicia a thread nem acessa o backend. `--no-esp32` não inicia o leitor do ESP32
(`--no-uart`, o nome antigo, continua valendo).
Para uma execução limitada, acrescente `--max-frames 120`. Ctrl+C ou SIGTERM encerram
os componentes antes de fechar o banco. A sincronização aguarda sua thread terminar;
se houver envio em andamento, o encerramento pode esperar o timeout da conexão.

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

- **Ao encerrar** (SIGTERM, Ctrl+C ou o fim normal da visão), uma linha de resumo. A média é a
  dos `fps` da visão, e a parte do ESP32 só aparece com ele ligado:

```
INFO vguard: resumo: visão 13.8 FPS em média; eventos: fadiga 1, distracao 1; visão reiniciada 0 vezes; ESP32: lost 0, corrupt 0; Pi até 61.2 °C, throttled sem avisos
```

### Bancada guiada da visão (passo 0 e passo C)

Na tela da Pi (Raspberry Pi Connect), depois da calibração:

```bash
cd ~/vguard/integracao
.venv/bin/python tools/vision_bench.py             # passo 0: 10 min, só a visão; pasta ../dados-integracao
.venv/bin/python tools/vision_bench.py --passo-c   # passo C: com o ESP32 (/dev/ttyUSB0) e o kill -9
```

A visão roda sem tela, como no critério. Uma janela à parte mostra em letra grande o que quem lê
a tela deve pedir, em voz alta, a quem está na frente da câmera; quem está na câmera olha sempre
para a frente, nunca para a tela. A sequência começa no **ESPAÇO** e dura 10 min:

1. `OLHE PARA A FRENTE` por 2 min, com a contagem até o próximo pedido;
2. `PREPARE-SE` por 5 s, e então `PECA PARA BOCEJAR`, até aparecer `OK! PODE FECHAR A BOCA`
   (o evento `fadiga` chegou ao log). Depois de 20 s sem o evento, a tela pede mais; em 60 s,
   `NAO DETECTOU`, e a sequência segue;
3. mais 2 min olhando para a frente, e `PECA PARA VIRAR O ROSTO`, do mesmo jeito (`distracao`);
4. `OLHE PARA A FRENTE` até o fim.

**N** adianta o passo (ou pula o pedido atual) e **Q** encerra antes. Um evento fora de um pedido
aparece em vermelho. O log inteiro vai para o terminal e para
`../dados-integracao/bancada-visao-<data>.log`, e o fim mostra cada critério com `OK` ou `FALHOU`.

No **passo C** (`--passo-c`, ou `--esp32 [PORTA]` e `--kill-vision`), o principal lê também o ESP32,
e depois do rosto virado vem `VISAO MORTA (KILL -9)`: a ferramenta mata a visão e espera
`OK! A VISAO VOLTOU`. No fim, ela confere no SQLite a telemetria dos 10 min: ~1 linha por segundo,
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
| `VGUARD_BACKEND` | `https://vguard.example/api` | URL do backend                        |
| `VGUARD_DEVICE`  | `vguard-01`                  | Identificador do dispositivo          |
| `VGUARD_TOKEN`   | vazio                        | Token de autenticação no backend      |

As redes Wi-Fi autorizadas ficam em `AUTHORIZED_WIFI` em `events/sync.py`.
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

## O que fica no SD

```
dados/
├── vguard.db          SQLite: telemetry, events, event_window
└── photos/<id>.jpg    foto de cada evento
```

Cada evento guarda `type`, `ts`, `score`, `data` (última amostra de `imu`/`obd`/`gps`)
e os estados de sincronização separados: `meta_synced` e `photo_synced` (RF18).

## Testes

Cada arquivo de teste roda sozinho. A partir de `integracao/`:

```bash
(cd vision && python3 test_metrics.py && python3 test_detector.py && python3 test_pipeline_integration.py && python3 test_fps.py)
python3 -B vision/test_capture.py
python3 -B vision/test_calibration.py
(cd vision && python3 test_protocol.py && python3 test_program.py)
```

```bash
(cd events && python3 test_event_manager.py && python3 test_sync.py && python3 test_esp32_reader.py && python3 test_gps_reader.py && python3 test_pi_monitor.py && python3 test_vision_process.py)
python3 -B test_vguard.py
(cd tools && python3 test_vision_bench.py)
```

Se já estiver dentro de `vision/` ou `events/`, rode só os `python3 test_*.py`.

`test_pipeline_integration.py` usa um mock do `EventManager` com a mesma assinatura do real.
`events/test_vision_process.py` usa uma visão falsa (`events/fake_vision.py`) que fala o protocolo do canal;
`vision/test_program.py` roda o programa da visão de verdade com um vídeo sintético (precisa do venv da visão).
O `test_vguard.py` confere que o principal não importa o OpenCV, o MediaPipe nem o NumPy.
`events/test_esp32_reader.py` e `events/test_gps_reader.py` usam um ESP32 e um receptor falsos num pty.
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
