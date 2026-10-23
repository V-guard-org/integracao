# V-GUARD — Integração (visão + gerenciador de eventos)

Junta os dois repositórios em um **único processo** na Raspberry Pi 4:

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
 ESP32 ──UART──► UartReader ──manager.update("imu"|"obd")─┘      │
                                                                 ▼
                                          Sync ◄── pending_* / ack_* ──► backend
```

1. **Visão** (thread principal; o `cv2.imshow` exige isso) lê um frame, extrai as métricas e passa ao
   `DriverStateDetector`. Quando uma condição dura o suficiente (olhos fechados ≥ 1 s,
   bocejo ≥ 1,5 s, cabeça desviada ≥ 2 s), o detector devolve `should_record=True` **uma vez por episódio**.
2. Nesse frame, a visão codifica a imagem **sem overlays** em JPEG e chama `record_event`.
3. **EventManager** anexa a última amostra de cada sensor, grava a foto e a linha no SQLite
   e guarda a janela de telemetria de 10 s antes e 10 s depois do evento (RF17).
4. **UartReader** (thread) decodifica o protocolo do ESP32 e alimenta `manager.update()` com IMU e OBD-II.
5. **Sync** (thread) envia a cada 60 s o que está pendente: metadados e telemetria por 4G ou Wi-Fi,
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

```bash
pip install -r vision/requirements.txt
python3 vision/scripts/download_model.py     # baixa vision/models/face_landmarker.task (se faltar)
```

```bash
python3 vguard.py --headless                 # na Pi, sem janela
```

```bash
python3 vguard.py --source video.mp4         # no notebook, com HUD (q para sair)
```

Outras opções: `--source 0` (índice da câmera), `--width`/`--height` (padrão 640×480).

Sem o ESP32 conectado, o `UartReader` só registra um aviso a cada 2 s e tenta de novo.
A visão e a gravação de eventos continuam funcionando.

### Variáveis de ambiente

| Variável         | Padrão                       | Uso                                  |
|------------------|------------------------------|--------------------------------------|
| `VGUARD_DATA`    | `dados`                      | Pasta no cartão SD                    |
| `VGUARD_UART`    | `/dev/ttyAMA5`               | UART ligada ao ESP32                  |
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

Cada arquivo de teste roda sozinho, de dentro da sua pasta:

```bash
cd vision && python3 test_metrics.py && python3 test_detector.py && python3 test_pipeline_integration.py
```

```bash
cd events && python3 test_event_manager.py && python3 test_sync.py
```

`test_pipeline_integration.py` usa um mock do `EventManager` com a mesma assinatura do real.
`events/test_uart_reader.py` precisa do ESP32 simulado em `../firmware_telemetria/host/fake_esp32.py`
(ou `VGUARD_FIRMWARE=<pasta>`).

## Pendências

- **GPS (RF02):** nada chama `manager.update("gps", ...)` ainda, então os eventos saem sem posição.
  Falta um leitor do GPS no mesmo formato do `UartReader`.
- **Cópias duplicadas:** `../visao` e `../gerenciador_eventos` ainda existem. As correções devem
  ser feitas aqui; o melhor é apagar ou arquivar as originais para não divergirem.
