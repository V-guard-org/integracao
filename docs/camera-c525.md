# Webcam Logitech C525 na aplicação integrada

Branch `webcam`: a visão usa a webcam USB **Logitech C525** (HD 720p,
foco automático) como câmera padrão. A câmera noturna OV5647 fica no branch
`cam_rasp`.

## Limites desta câmera

- **Sem visão noturna.** Como as webcams comuns, a C525 não enxerga bem o
  infravermelho e não tem LEDs IR:
  com o carro escuro o rosto some e não há detecção. A lista de materiais pede
  uma câmera "sem filtro IR e com iluminadores infravermelhos"; a C525 serve
  para bancada, testes de dia e demonstração, não para a operação noturna.
- **Foco automático** pode "respirar" quando o rosto se move muito perto da lente.
  Monte a câmera a uma distância fixa do rosto (cerca de 50 a 80 cm).
- **720p é o máximo**, e a visão usa 640×480: a mais na Raspberry Pi só deixa o
  MediaPipe mais lento, sem melhorar a detecção.

## Ligar e conferir

Ligue numa porta USB da Raspberry Pi (ou do notebook) e confira:

```bash
lsusb | grep 046d
```

```bash
v4l2-ctl --list-devices
```

A C525 aparece como `046d:0826 Logitech` e como `/dev/video0` (o `v4l2-ctl`
vem no pacote `v4l-utils`). Se houver outra câmera, o índice pode mudar entre
reinícios; use o nome fixo que aparece em:

```bash
ls /dev/v4l/by-id/
```

## Executar

A fonte padrão já é a webcam 0:

```bash
python3 vguard.py --headless
```

Com outra câmera ligada, aponte para o nome fixo da C525:

```bash
python3 vguard.py --headless --source /dev/v4l/by-id/<nome-da-c525>-video-index0
```

Calibração do motorista, na tela da Pi:

```bash
bash tools/test_camera_gui.sh --calibrate
```

O perfil de calibração guarda a câmera usada. **Ao trocar entre a C525 e a
OV5647, refaça a calibração.**

## O que muda em relação à câmera CSI

- **Captura pelo V4L2 direto** (no Linux), com resolução e FPS pedidos à câmera
  e **1 único buffer**: a inferência na Pi (~14 FPS) é mais lenta que a câmera
  (30 FPS), e com um buffer cada leitura pega o quadro mais novo, sem atraso
  acumulado.
- **Cabo solto ou câmera travada é erro.** Uma webcam não tem "fim de vídeo":
  se ela parar de enviar quadros, o `vguard.py` sai com erro
  (`A webcam ... parou de enviar quadros`), para um serviço do sistema reiniciar
  o processo. Vídeo gravado continua terminando normalmente.
- Não precisa de `picamera2`, `libcamera` nem `--camera-python`.

## Testes

```bash
python3 -B vision/test_capture.py
```

```bash
python3 -B vision/test_pipeline_integration.py
```

```bash
python3 -B test_vguard.py
```

Conferem que a webcam abre pelo V4L2 com 1 buffer, que o caminho
`/dev/v4l/by-id/...` é tratado como webcam, que a C525 é a fonte padrão e que a
webcam parar vira erro, enquanto o fim de um vídeo não.

Com a câmera ligada, a gravação real de um evento pode ser conferida com:

```bash
python3 -B tools/validate_camera_persistence.py --frames 120 --data-dir /tmp/vguard-c525
```
