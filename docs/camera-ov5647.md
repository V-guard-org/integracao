# Câmera noturna OV5647 na aplicação integrada

O módulo `vision/` já faz parte deste repositório. A câmera da foto fornecida é
anunciada como OV5647 de 5 MP, com cabo CSI e dois iluminadores infravermelhos.
O anúncio mistura ângulos de visão e variantes; não usamos esses números como
calibração óptica nem presumimos uma interface GPIO para LEDs ou IR-CUT.

`vguard.py` usa `--source picamera2` por padrão. `vision/capture.py` entrega frames
BGR ao pipeline existente. No Picamera2/libcamera o formato `RGB888` corresponde
a bytes B,G,R no array: o pipeline converte uma única vez para RGB ao entrar no
MediaPipe. O JPEG de evento continua saindo do frame sem HUD, pelo EventManager.
A captura solicita 640×480 a 30 FPS, com exposição automática. O FPS efetivo
também depende da inferência, iluminação e sensor; os 5 MP não são necessários
para processar cada frame na Pi 4.

## 1. Conexão e reconhecimento no próprio Raspberry Pi

Desligue a Pi e desconecte a alimentação antes de conectar o cabo CSI. Na Pi 4B,
use o conector **CAMERA**, com o cabo adequado à placa. Depois de ligar:

```bash
cat /etc/os-release
python3 --version
rpicam-hello --list-cameras
rpicam-still --nopreview --timeout 3000 --output /tmp/ov5647-check.jpg
```

Em sistemas antigos os comandos podem se chamar `libcamera-hello` e
`libcamera-still`. A lista deve reconhecer o sensor como `ov5647`. Se não listar,
verifique cabo/conector e alimentação antes de abrir a aplicação. Feche outros
processos que estejam usando a câmera.

Alguns módulos de terceiros não são detectados automaticamente. Apenas nesse
caso, siga a configuração oficial do sensor: no Raspberry Pi OS Bookworm, em
`/boot/firmware/config.txt`, desative a detecção automática com
`camera_auto_detect=0` e use `dtoverlay=ov5647`; depois reinicie. Não duplique
overlays existentes. Em sistemas antigos o arquivo pode ser `/boot/config.txt`.

## 2. Picamera2 e o ambiente Python

Instale a biblioteca pela distribuição, para receber bindings compatíveis com
libcamera:

```bash
sudo apt install python3-picamera2
```

O ambiente já documentado do V-GUARD usa **Python 3.11** e MediaPipe 0.10.18.
Antes de modificar qualquer ambiente, confira o Python do sistema e o Python
usado pelo projeto. Os bindings `libcamera` instalados pelo apt precisam ter a
mesma versão menor do Python do projeto. Um pacote compilado para Python 3.13
não funciona no Python 3.11; `--system-site-packages` não corrige essa diferença.

Se o sistema também usa Python 3.11, um ambiente separado para esta validação
pode ser criado a partir da raiz do repositório, preservando o ambiente existente:

```bash
/usr/bin/python3.11 -m venv --system-site-packages .venv-csi
.venv-csi/bin/python -m pip install -r requirements-pi4-py311.txt
.venv-csi/bin/python -c 'from picamera2 import Picamera2; import libcamera, cv2, mediapipe; print("Imports CSI/visão OK")'
.venv-csi/bin/python vision/scripts/download_model.py
```

No Debian 13 Trixie com Python de sistema 3.13, use
`--camera-python /usr/bin/python3`. Nesse modo, apenas a captura roda num auxiliar
com os bindings do sistema. Um pipe transmite frames BGR sem compressão para a
aplicação 3.11. A inferência e os eventos continuam na aplicação principal; o
auxiliar é encerrado junto dela, inclusive em erro. Não é necessário recompilar
libcamera ou trocar de sistema operacional.

Para criar o ambiente 3.11 num sistema que só tem Python 3.13, use uma pasta
isolada para o bootstrap (a partir da raiz do repositório):

```bash
python3 -m venv ../bootstrap
../bootstrap/bin/pip install uv
../bootstrap/bin/uv venv --python 3.11 .venv
../bootstrap/bin/uv pip install --python .venv/bin/python -r requirements-pi4-py311.txt
/usr/bin/python3 -c 'from picamera2 import Picamera2; import libcamera; print("Câmera OK")'
.venv/bin/python -c 'import cv2, mediapipe; print("Visão OK")'
.venv/bin/python vision/scripts/download_model.py
source .venv/bin/activate
```

Os comandos de câmera real abaixo incluem o auxiliar usado na Pi Trixie. Quando
ambos os pacotes estão no mesmo interpretador, omita `--camera-python`. Não
instale outra distribuição OpenCV junto de `opencv-contrib-python` na mesma venv.

## 3. Testes automatizados na Pi, sem webcam no PC

Na raiz `integracao/` e com o ambiente da Pi ativado:

```bash
python3 -B vision/test_capture.py
python3 -B vision/test_metrics.py
python3 -B vision/test_detector.py
python3 -B vision/test_pipeline_integration.py
python3 -B test_vguard.py
python3 -B events/test_event_manager.py
python3 -B events/test_sync.py
```

Esses testes verificam contratos, limpeza de recursos, processamento e eventos.
O teste de pipeline cria um vídeo sintético na própria Pi; não precisa de webcam
no computador. Os testes de captura usam um substituto Picamera2 e não comprovam
que o cabo/sensor funciona. O teste real vem a seguir.

## 4. Câmera real → MediaPipe → detector → JPEG → SQLite

Primeiro execute uma sessão curta com a câmera real e métricas reais, sem backend
nem ESP32, em uma pasta de dados exclusiva do teste:

```bash
VGUARD_DATA=/tmp/vguard-ov5647-live \
  python3 -B vguard.py --headless --source picamera2 \
  --camera-python /usr/bin/python3 \
  --no-uart --no-sync --max-frames 120
```

Depois execute a validação de persistência. A pasta passada em `--data-dir`
**precisa ser nova** a cada execução:

```bash
python3 -B tools/validate_camera_persistence.py --source picamera2 \
  --camera-python /usr/bin/python3 \
  --frames 120 --data-dir /tmp/vguard-ov5647-persistencia-dia
```

Esse segundo teste injeta métricas sintéticas de olhos fechados no detector para
provocar exatamente um evento identificado como `teste_integracao_sintetico_fadiga`.
A câmera, o MediaPipe, o JPEG e a persistência são reais. O teste confere o JPEG,
integridade do SQLite e estados de sincronização e salva `result.json` com FPS,
temperatura e quantidade de frames com métricas faciais reais. Um resultado com
zero rostos exige investigar enquadramento/foco/iluminação; o evento sintético
não comprova a acurácia da detecção facial ou de fadiga.

## 5. Teste noturno e ajustes

Com iluminação IR efetivamente ativa na placa, repita em pouca luz:

```bash
python3 -B tools/validate_camera_persistence.py --source picamera2 --infrared \
  --camera-python /usr/bin/python3 \
  --frames 120 --data-dir /tmp/vguard-ov5647-persistencia-noite
```

`--infrared` desativa AWB e fixa os ganhos vermelho/azul em 1, mantendo exposição
automática; é um perfil inicial para experimentar sob IR. Sem a opção, o balanço
de branco permanece automático para uso diurno. `--fps 15` permite testar uma
cadência menor se a exposição noturna for insuficiente. Isso não substitui luz IR.

O acionamento dos iluminadores e do filtro IR-CUT depende do módulo comprado.
Não há acionamento por GPIO neste código sem pinagem/interface confirmadas.
A OV5647 não fornece autofoco; ajuste o foco mecanicamente, se a lente permitir,
com o rosto na distância de uso. Confirme o enquadramento na foto de teste.

A lente grande angular pode distorcer o rosto, principalmente nas bordas. Os
limiares de EAR/MAR/pose em `vision/config.py` são referências; `solvePnP` ainda
usa uma matriz de câmera aproximada e distorção zero. Valide rosto centralizado,
olhos abertos/fechados, bocejo e cabeça desviada durante o dia e sob IR antes de
usar os eventos como detecção validada. Não há calibração óptica medida da câmera
nem garantia de acurácia noturna nesta adaptação.

Para acompanhar o HUD na tela conectada à Pi, retire `--headless`. Para uma câmera
CSI diferente use `--camera-num` conforme a lista de sensores. Webcam USB continua
disponível com `--source 0`; vídeo com `--source arquivo.mp4`.

## Referências

- [Instalação, libcamera, OV5647 e rpicam-apps — Raspberry Pi](https://www.raspberrypi.com/documentation/accessories/camera-software.html)
- [Manual Picamera2 — formatos, configuração e controles](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf)
- [Formatos de canais no Picamera2](https://github.com/raspberrypi/picamera2/blob/main/picamera2/request.py)
