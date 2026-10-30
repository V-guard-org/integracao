# V-GUARD — Integração (visão + gerenciador de eventos)

Junta visão e gerenciador em um **processo principal** na Raspberry Pi 4.
Quando a câmera usa outro Python, somente a captura roda em um auxiliar:

| Pasta     | Origem                                       | Papel                                                        |
|-----------|----------------------------------------------|--------------------------------------------------------------|
| `vision/` | `visao/edge/vguard/vision`                   | Câmera → MediaPipe → EAR/MAR/pose → decide *quando* há evento |
| `events/` | `gerenciador_eventos/edge/vguard/events`     | Grava telemetria e eventos no SD e sincroniza com o backend  |
| `vguard.py` | novo                                       | Ponto de entrada: cria tudo e liga as peças                  |

As duas partes não se importam entre si. Quem as conecta é só o `vguard.py`.

## Como funciona

```
                 thread principal                     threads em segundo plano
 câmera ──► VisionPipeline (vision/main.py)
              │ detector.py: should_record=True
              │ frame → JPEG em memória
              ▼
        manager.record_event(type, score, jpeg, ts) ──► EventManager ──► SD (dados/)
                                                          ▲      │
 ESP32 ──UART──► UartReader ──manager.update("imu"|"obd")─┤      │
 GNSS ───NMEA──► GpsReader ───manager.update("gps")───────┘      │
                                                                 ▼
                                          Sync ◄── pending_* / ack_* ──► backend
```

1. **Visão** (thread principal; o `cv2.imshow` exige isso) lê um frame, extrai as métricas e passa ao
   `DriverStateDetector`. Quando uma condição dura o suficiente (bocejo ≥ 1,5 s ou cabeça desviada ≥ 2 s), o detector devolve `should_record=True` **uma vez por episódio**.
2. Nesse frame, a visão codifica a imagem **sem overlays** em JPEG e chama `record_event`.
3. **EventManager** anexa a última amostra de cada sensor, grava a foto e a linha no SQLite
   e guarda a janela de telemetria de 10 s antes e 10 s depois do evento (RF17).
4. **UartReader** (thread) decodifica o protocolo do ESP32 e alimenta `manager.update()` com IMU e OBD-II.
5. **GpsReader** (thread) lê as frases RMC do receptor GNSS e grava `{"lat", "lon", "speed"}` (km/h)
   em `manager.update("gps")`. Sem fix, grava `{}`, para o evento não levar uma posição velha.
6. **Sync** (thread) envia a cada 60 s o que está pendente: metadados e telemetria por 4G ou Wi-Fi,
   fotos **só** por Wi-Fi autorizado. Um item só é marcado como enviado depois do 2xx do backend (RF22).

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
  devolve `None`, a visão não conta o evento.
- O `events/` usa só a biblioteca padrão do Python. Toda a dependência pesada (OpenCV, MediaPipe) está em `vision/`.

## Executar

Todos os comandos abaixo partem de `integracao/`.

```bash
pip install -r vision/requirements.txt
python3 vision/scripts/download_model.py     # baixa vision/models/face_landmarker.task (se faltar)
```

> **Branch `cam_rasp`:** a câmera padrão deste branch é a **câmera noturna
> OV5647** (CSI, 5 MP, 2 LEDs infravermelhos). Preparo, LEDs e teste noturno em
> [docs/camera-ov5647.md](docs/camera-ov5647.md). A webcam Logitech C525 fica no
> branch `webcam`.

```bash
python3 vguard.py --headless                 # na Pi, câmera CSI OV5647 via Picamera2, sem janela
```

```bash
python3 vguard.py --source video.mp4         # no notebook, com HUD (q para sair)
```

Outras opções: `--source 0` (webcam USB), `--width`/`--height` (padrão 640×480),
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

Com o modelo em `vision/models/face_landmarker.task`, execute:

```bash
cd /home/vguard/vguard/integracao
VGUARD_DATA=/home/vguard/vguard/dados-integracao \
  /home/vguard/vguard/compat/mediapipe-0.10.18-py311/.venv/bin/python -B \
  vguard.py --headless --source picamera2 --no-uart --no-sync
```

Para CSI no mesmo processo, esse interpretador precisa importar `picamera2` e `libcamera`.
No Debian 13 com Python de sistema 3.13, use `--camera-python /usr/bin/python3`:
a câmera roda em um auxiliar 3.13 e envia frames BGR ao pipeline MediaPipe 3.11.
`--source 0` continua selecionando uma webcam USB.

`--no-sync` não inicia a thread nem acessa o backend. `--no-uart` não inicia o leitor.
Para uma execução limitada, acrescente `--max-frames 120`. Ctrl+C ou SIGTERM encerram
os componentes antes de fechar o banco. A sincronização aguarda sua thread terminar;
se houver envio em andamento, o encerramento pode esperar o timeout da conexão.

Sem o ESP32 ou o GPS conectados, o leitor correspondente só registra um aviso a cada 2 s e
tenta de novo. A visão e a gravação de eventos continuam funcionando.

### Variáveis de ambiente

| Variável         | Padrão                       | Uso                                  |
|------------------|------------------------------|--------------------------------------|
| `VGUARD_DATA`    | `dados`                      | Pasta no cartão SD                    |
| `VGUARD_UART`    | `/dev/ttyAMA5`               | UART ligada ao ESP32                  |
| `VGUARD_GPS`     | `/dev/serial0`               | UART ligada ao receptor GNSS          |
| `VGUARD_GPS_BAUD`| `9600`                       | Velocidade da UART do GNSS            |
| `VGUARD_BACKEND` | `https://vguard.example/api` | URL do backend                        |
| `VGUARD_DEVICE`  | `vguard-01`                  | Identificador do dispositivo          |
| `VGUARD_TOKEN`   | vazio                        | Token de autenticação no backend      |

As redes Wi-Fi autorizadas ficam em `AUTHORIZED_WIFI` em `events/sync.py`.
Os limiares da visão (EAR, MAR, yaw, pitch e tempos) ficam em `vision/config.py`.

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
(cd vision && python3 test_metrics.py && python3 test_detector.py && python3 test_pipeline_integration.py)
python3 -B vision/test_capture.py
python3 -B vision/test_calibration.py
```

```bash
(cd events && python3 test_event_manager.py && python3 test_sync.py && python3 test_uart_reader.py && python3 test_gps_reader.py)
python3 -B test_vguard.py
```

Se já estiver dentro de `vision/` ou `events/`, rode só os `python3 test_*.py`.

`test_pipeline_integration.py` usa um mock do `EventManager` com a mesma assinatura do real.
`events/test_uart_reader.py` precisa do ESP32 simulado em `../firmware_telemetria/host/fake_esp32.py`
(ou `VGUARD_FIRMWARE=<pasta>`). `events/test_gps_reader.py` usa um receptor falso num pty.
Os testes de `events/` precisam de Linux (na Pi ou no WSL).

Teste opt-in da aplicação com câmera CSI e persistência reais, sem UART/backend,
executado na Raspberry Pi:

```bash
python3 -B tools/validate_camera_persistence.py --source picamera2 \
  --data-dir /caminho/novo-exclusivo-do-teste
```

O teste usa o ponto de entrada integrado e injeta métricas sintéticas no detector temporal.
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
