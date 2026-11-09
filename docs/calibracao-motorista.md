# Calibração do motorista com câmera lateral

A referência é a postura **olhando para a estrada**, mesmo que o rosto apareça
inclinado em relação à câmera junto ao retrovisor. Faça a coleta com o veículo
parado (ou na bancada), câmera firme e iluminação de teste já definida.
Esta calibração compensa a orientação da montagem; não mede a distorção da lente.

## Abrir na tela da Raspberry Pi

No terminal da Pi, pelo compartilhamento de tela do Raspberry Pi Connect:

```bash
cd ~/vguard/integracao
bash tools/test_camera_gui.sh --calibrate
```

O script usa `.venv/bin/python`, captura CSI pelo Python do sistema, perfil IR,
interface X11/XWayland e desativa UART, GPS e sincronização. O perfil fica em
`../dados-teste-gui/calibration-driver.json`, junto dos dados locais de teste.
Para limitar a duração acrescente `--max-frames 1200`; Q encerra normalmente.
O processo não inicia a câmera até esse comando ser executado.

## Coletar a referência

A janela abre no canto de cima da tela. À esquerda fica a câmera, só com o painel de
medidas do canto. À direita, uma coluna mostra o pedido da vez em letra grande, como
na bancada guiada (`tools/vision_bench.py`). Os papéis são dois:

- **Na câmera:** a pessoa senta como se dirigisse e olha sempre para a frente, nunca
  para a tela. Ela só faz o que for pedido.
- **Na tela:** quem está no computador lê o pedido em voz alta e aperta as teclas.

Antes de começar, fixe a câmera na posição de uso, junto ao retrovisor, sem segurar com
a mão, e clique na janela para ela receber as teclas. Depois:

1. **PRONTOS?** Aperte **ESPAÇO**.
2. **Passo 1 de 3, postura.** "PECA PARA OLHAR PARA A FRENTE", com a contagem "A medida
   comeca em 3". Em seguida vem "MEDINDO A POSTURA", com uma barra de 3 s, e por fim
   "OK! POSTURA MEDIDA". Olhos abertos, boca fechada, cabeça parada.
3. **Passo 2 de 3, olhos.** Começa sozinho, logo depois do OK.
   - A tela mostra "PECA PARA FECHAR OS OLHOS", depois "MEDINDO OS OLHOS" e por fim
     "OK! PODE ABRIR OS OLHOS".
   - Quem está na câmera fica de olhos fechados até ouvir o "pode abrir".
   - **N** pula os olhos e grava só a postura.
4. **O perfil é gravado sozinho** no fim dos olhos. Não há tecla de salvar.
5. **Passo 3 de 3, conferência.** Também começa sozinha (abaixo).

Nenhum evento é gravado durante a calibração e a conferência. **C** recomeça a
calibração do início, e **Q** sai. A referência anterior no arquivo só é substituída
quando o perfil novo é gravado.

Se algo der errado, a coluna fica vermelha e diz o que houve e o que fazer:

| Na tela | O que houve | O que fazer |
|---|---|---|
| MEDIR DE NOVO | A cabeça mexeu durante a medida da postura | ESPAÇO e ficar parado olhando para a frente |
| OLHOS NAO MEDIDOS | Olhos abertos e fechados ficaram parecidos, ou a cabeça mexeu de olhos fechados | ESPAÇO mede de novo; N pula os olhos |
| ROSTO FORA DA IMAGEM | O rosto sumiu no meio da medida | Voltar à posição: a medida recomeça sozinha |
| FALHA AO GRAVAR | O perfil não pôde ser gravado no arquivo | ESPAÇO tenta de novo |

Com FPS muito baixo, a medida pode levar mais de 3 s, porque precisa de pelo menos 20 quadros.

## Conferência guiada

Logo depois de gravar o perfil, a coluna guia três provas, para conferir que a
calibração funcionou. **Nenhuma prova grava evento.** Cada prova tem:

1. um aviso de 3 s, "PREPARE-SE: daqui a 3 s, pedir para ...", com o detector zerado;
2. o pedido, sem contagem, até aparecer o OK. Depois de 8 s sem passar, a tela pede
   mais. Em 20 s sem passar, fica `NAO DETECTOU`, e a prova seguinte começa;
3. o resultado por 2,5 s: "OK! PODE VOLTAR", "OK! PODE FECHAR A BOCA" ou
   "NAO DETECTOU".

| Prova | Pedido na tela | Passa quando | Retorno ao vivo |
|---|---|---|---|
| 1 de 3, lado | PECA PARA VIRAR O ROSTO | o estado vira `DISTRAIDO` (mais de 25° por 2 s) | `Lado: 12 de 25 graus` |
| 2 de 3, bocejo | PECA PARA BOCEJAR | o estado vira `FADIGA` (MAR acima de 0,60 por 1,5 s) | `Boca: 0.45 de 0.60` |
| 3 de 3, estrada | PECA PARA FICAR PARADO | `NORMAL` por 3 s seguidos | `Parado: 1.2 de 3 s` |

- **N** pula a prova da vez, que conta como não passada.
- **No fim:** "CALIBRACAO PRONTA" (verde) se as três passaram, ou "CONFERIR" (vermelho)
  se alguma não passou, com o resumo (`Lado OK | Bocejo OK | Estrada OK`).
- **Depois disso, os eventos voltam a ser gravados.** ESPAÇO confere de novo, C calibra
  de novo e Q sai.
- **Com um perfil já gravado** (sem `--calibrate`), a coluna mostra "MONITORANDO", e
  **V** (ou ESPAÇO) começa a conferência sem recalibrar. Serve para conferir um perfil de
  outro dia.

Para reutilizar o perfil salvo:

```bash
bash tools/test_camera_gui.sh
```

Recalibre se mudar motorista, posição da câmera, banco, distância ou iluminação.
Resolução e modo IR/fonte são conferidos no arquivo; alterações físicas não são
detectadas automaticamente. Para testar sem IR, use o comando manual abaixo
sem `--infrared`, com outro nome de perfil e nova calibração.

```bash
QT_QPA_PLATFORM=xcb VGUARD_DATA=../dados-teste-gui .venv/bin/python -B vguard.py --camera-python /usr/bin/python3 --infrared --no-esp32 --no-gps --no-sync --calibration-file ../dados-teste-gui/calibration-driver.json --calibrate
```

## O que mudou na decisão

- A pose usada para distração é relativa à referência: `R_neutra.T @ R_atual`.
  O HUD também mostra os valores brutos para comparação. Os limiares iniciais
  continuam sendo mais de 25° horizontal ou 20° vertical por 2 segundos.
- EAR baixo isolado deixou de disparar `FADIGA` e não existe estado `PISCANDO`.
  Fechamento ocular sem desvio da cabeça não gera evento nesta versão experimental.
- Bocejo sustentado mantém a regra anterior de `FADIGA` (MAR > 0,60 por 1,5 s).
  Esta calibração não valida nem ajusta o detector de bocejo.
- O sinal ocular só é usado se as distribuições abertas/fechadas se separarem
  em ambos os olhos. Medição ambígua, fora da faixa, olhos discordantes ou pose
  muito diferente da coleta dão `indeterminado`, com peso zero.
- Quando já há distração sustentada e sinal ocular baixo confiável por 1 segundo,
  o score recebe no máximo +0,05. Isso não antecipa nem cria o evento. `Reforco: sim`
  no HUD identifica esse caso. O score é uma heurística experimental, **não uma
  probabilidade de acerto**; sinais de pose e olhos podem compartilhar erros.
- Sem perfil ocular, a distração pela postura continua funcionando. O EAR bruto
  permanece visível, sem classificação isolada de olhos fechados.

As margens de separação e uso ocular são conservadoras e experimentais, não
medidas de confiança do MediaPipe: quantil 20% dos abertos deve superar quantil
90% dos fechados em pelo menos 0,025 e 20% da mediana aberta. O sinal só é usado
até 35° de yaw, 30° de pitch e 15° de roll relativos e yaw bruto menor que 65°.
A resolução/FOV da lente, oclusão e exposição forte podem continuar causando erros.

A pose vem de `solvePnP` com intrínsecos aproximados e distorção zero; a
[documentação do OpenCV](https://docs.opencv.org/4.13.0/d5/d1f/calib3d_solvePnP.html)
descreve o papel dos intrínsecos e da distorção. A referência pessoal não substitui
uma calibração óptica nem valida a acurácia no veículo.

## Conferir depois de calibrar

Com o veículo parado, confira 20–30 segundos por cenário:

| Cenário | Resultado esperado |
|---|---|
| Postura habitual, olhando à frente | `NORMAL`, pose relativa próxima de zero |
| Fechar olhos mantendo a cabeça na referência | Sem evento isolado; sinal ocular pode mudar |
| Desviar cabeça por menos de 2 s e voltar | Sem evento de distração |
| Desviar cabeça por mais de 2 s | `DISTRAIDO`, um evento por episódio |
| Um olho oculto ou medição ambígua | Sinal ocular indeterminado, sem reforço |

`NORMAL` significa ausência dos critérios de evento, não garantia de atenção.
O sistema estima orientação da cabeça, não direção precisa do olhar. Primeiro
confira a postura normal; não ajuste limiares apenas para fazer um único exemplo
passar. Os testes automatizados usam métricas/imagens sintéticas e não substituem
estas verificações com a câmera na montagem real.

Testes sem câmera, na Pi:

```bash
.venv/bin/python -B vision/test_calibration.py
.venv/bin/python -B vision/test_detector.py
.venv/bin/python -B vision/test_pipeline_integration.py
.venv/bin/python -B test_vguard.py
```

## Verificação de software em 29/10/2026

Executada na Raspberry Pi 4B, Python 3.11.18, em diretório temporário, sem abrir
a câmera física: 10 testes de calibração, 8 do detector, métricas geométricas,
4 cenários de pipeline, 8 da aplicação e 15 da captura passaram. A suíte reproduz
os valores dos dois exemplos de postura normal fornecidos pelo operador e
verifica ausência de evento após aplicar a referência lateral. Isso verifica
a lógica com entradas controladas; a nova calibração ainda precisa ser coletada
e validada com o motorista e a câmera na posição real.
