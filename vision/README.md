# V-GUARD — Visão Computacional

Módulo de visão computacional do projeto V-GUARD.

A primeira versão utiliza o MediaPipe Face Landmarker para realizar a detecção de landmarks faciais e extrair métricas utilizadas na análise do estado do motorista:

- EAR (Eye Aspect Ratio)
- MAR (Mouth Aspect Ratio)
- Yaw, Pitch e Roll (orientação da cabeça)

A implementação inicial utiliza Python e OpenCV e pode ser executada utilizando uma webcam.

## Requisitos

- Python 3.10 ou 3.11
- Webcam
- pip
- Git (opcional)

Verifique a versão do Python:

```bash
python --version
```
No Linux, caso python não esteja disponível:
```bash
python3 --version
```


### Estrutura do projeto
```
edge/vguard/vision/
│
├── config.py                       # Constantes, índices MediaPipe, modelo 3D canônico e limiares
├── metrics.py                      # Funções matemáticas puras (EAR, MAR em pixels e solvePnP)
├── detector.py                     # Análise temporal, máquina de estados, histerese e cooldown
├── main.py                         # Orquestrador do pipeline (suporte a GUI e modo headless)
├── test_metrics.py                 # Testes unitários de EAR, MAR, Head Pose e aspect ratio
├── test_detector.py                # Testes de validação temporal do detector
├── test_pipeline_integration.py    # Teste de integração end-to-end com Mock do EventManager
├── requirements.txt                # Dependências do módulo
├── README.md                       # Documentação do módulo
│
├── scripts/
│   └── download_model.py           # Utilitário para download do modelo MediaPipe
│
└── models/
    └── face_landmarker.task        # Modelo do MediaPipe Face Landmarker
```

### Para executá-lo:
#### 1. Criar o ambiente virtual

Clone o projeto ou entre na pasta do módulo:

```bash
cd edge/vguard/vision
```

Crie o ambiente virtual:

Windows:
```
python -m venv .venv
```
Linux:
```
python3 -m venv .venv
```

#### 2. Ativar o ambiente virtual
Windows — PowerShell:
```
.venv\Scripts\Activate.ps1
```
Windows — CMD:
```
.venv\Scripts\activate
```
Linux
```bash
source .venv/bin/activate
```

Após a ativação, o terminal deverá indicar o ambiente `.venv` ativo.

#### 3. Atualizar o pip
```bash
python -m pip install --upgrade pip
```
#### 4. Instalar as dependências

Com o ambiente virtual ativado:
```bash
pip install -r requirements.txt
```
As dependências utilizadas atualmente são:
```
mediapipe==0.10.35
opencv-python==4.11.0.86
numpy==2.1.3
```
#### 5. Modelo do MediaPipe

O projeto utiliza o modelo `face_landmarker.task` localizado em `models/face_landmarker.task`.

Para baixá-lo automaticamente através do script utilitário:
```bash
python scripts/download_model.py
```

O script criará o diretório `models/` e fará o download do modelo oficial do MediaPipe.

#### 6. Executar o Pipeline

Com o ambiente virtual ativado:

**Modo com Interface Gráfica / Depuração:**
```bash
python main.py
```
O programa abrirá a webcam e exibirá o overlay com EAR, MAR, Head Pose, Estado e FPS.
Pressione **Q** para encerrar.

**Modo Headless (Sem interface gráfica — recomendado para Raspberry Pi):**
```bash
python main.py --headless
```

**Executar a partir de arquivo de vídeo gravado:**
```bash
python main.py --source video_teste.mp4
```

#### 7. Testes Automatizados

O módulo conta com testes determinísticos independentes:

```bash
# 1. Teste de métricas puras (EAR, MAR e solvePnP em pixels):
python test_metrics.py

# 2. Teste temporal do detector (histerese, debounce e estados):
python test_detector.py

# 3. Teste de integração ponta a ponta com Mock do EventManager:
python test_pipeline_integration.py
```

#### 8. Arquitetura do Pipeline

```
Câmera (640x480)
   ↓
MediaPipe Face Landmarker (timestamps monotônicos reais)
   ↓
Landmarks Faciais (478 pontos)
   ↓
metrics.py (EAR + MAR desnormalizados em pixels + solvePnP)
   ↓
detector.py (Análise temporal, histerese 0.15s, cooldown 5.0s)
   ↓
EventManager.record_event(type, score, jpeg, ts) [codificação sob demanda]
```

- **EAR**: abertura ocular média com correção de aspecto em pixels.
- **MAR**: abertura bucal para detecção de bocejos sustentados (diferenciando de fala).
- **Head Pose (solvePnP + RQDecomp3x3)**: Pitch, Yaw e Roll estáveis em graus para análise de distração.
- **Integração V-GUARD**: quando `detection.should_record` for `True`, o frame limpo é codificado em JPEG sob demanda e encaminhado ao `EventManager` com `score` de severidade contínua.

#### 9. Desativar o ambiente virtual

Quando terminar o desenvolvimento:
```bash
deactivate
```
Para trabalhar novamente no projeto, basta ativar o ambiente:
Windows:
```powershell
.venv\Scripts\Activate.ps1
```
Linux:
```bash
source .venv/bin/activate
```