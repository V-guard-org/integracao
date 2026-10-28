# Validação da câmera OV5647 na Raspberry Pi 4B

Testes executados no hardware com a aplicação integrada, pela conexão SSH.
Fotos, banco e relatórios completos foram mantidos nas pastas exclusivas de teste
na Pi. Este documento registra apenas os resultados agregados.

## Ambiente observado

- Raspberry Pi 4 Model B Rev 1.5, ARM64, Cortex-A72, 4 GB de RAM.
- Debian GNU/Linux 13 Trixie; Python de sistema 3.13.5.
- Sensor reconhecido por `rpicam-hello --list-cameras`: **ov5647**, 2592×1944.
- Picamera2 0.3.38 e libcamera 0.7.2 instalados pelo sistema.
- Ambiente isolado Python 3.11.18 com MediaPipe 0.10.18, NumPy 1.26.4,
  OpenCV contrib 4.11.0.86 e JAX/JAXlib 0.4.35.
- Captura com `/usr/bin/python3` usando `--camera-python`; frames BGR 640×480,
  cadência solicitada de 30 FPS, inferência no processo principal 3.11.
- UART e sincronização desativadas nos testes de câmera real.

## Testes automatizados

Passaram na própria Raspberry Pi:

- `vision/test_capture.py`: 15 testes, incluindo CSI, perfil IR, leitura do pipe,
  validação do cabeçalho, falhas e encerramento do auxiliar.
- `vision/test_metrics.py`: EAR, MAR, pose e correção da proporção em pixels.
- `vision/test_detector.py`: 7 cenários temporais.
- `vision/test_pipeline_integration.py`: vídeo sintético, MediaPipe real, evento,
  captura CSI substituta e preservação das cores BGR no JPEG.
- `test_vguard.py`: 7 testes de opções, inicialização e encerramento.
- `events/test_event_manager.py` e `events/test_sync.py`.

## Câmera e persistência reais

Uma captura inicial com `rpicam-still` gerou um JPEG real da OV5647. Depois a
aplicação integrada processou 120 frames reais sem UART/backend e encerrou.
A validação de persistência foi repetida com o rosto enquadrado:

| Condição | Frames | Frames com métricas faciais reais | Tempo total | FPS efetivo | Temperatura final |
|---|---:|---:|---:|---:|---:|
| Iluminação inicial, AWB automático | 360 | 360 (100%) | 25,455 s | 14,14 | 50,1 °C |
| Pouca luz e IR ligado, conforme informado pelo operador; `--infrared` | 360 | 360 (100%) | 25,893 s | 13,90 | 50,1 °C |

Em cada execução houve exatamente um evento sintético identificado,
`teste_integracao_sintetico_fadiga`, e uma foto JPEG real 640×480 sem HUD.
O SQLite passou em `PRAGMA integrity_check`, a janela do evento foi fechada e
`meta_synced=photo_synced=0`. Os JPEGs tinham 40.074 e 37.444 bytes,
respectivamente. As duas imagens foram inspecionadas visualmente: rosto e olhos
visíveis; a captura sob o perfil IR apresentou menor dominante de cor.

Os FPS incluem inicialização e processamento, não são a cadência do sensor.
O pico de RSS registrado pelo script se refere ao processo principal; não inclui
a memória do auxiliar de captura.

## Limites desta validação

Os frames e a detecção de landmarks foram reais. O disparo de fadiga foi
**provocado com métricas sintéticas**, para testar a persistência de forma
determinística. Não foi medida acurácia de fadiga, bocejo ou distração, nem foram
calibrados os limiares ou a lente grande angular. Os resultados se referem a uma
pessoa, enquadramento e ambiente; não constituem avaliação de acurácia geral.

O guia [camera-ov5647.md](camera-ov5647.md) contém os comandos para repetir os
testes. Para o Trixie, o argumento essencial é `--camera-python /usr/bin/python3`.
