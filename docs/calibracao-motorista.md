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

1. Fixe a câmera na posição pretendida junto ao retrovisor, sem segurar com a mão.
   Sente na postura habitual, mantendo o rosto enquadrado e os olhos visíveis.
   Escolha um ponto à frente que represente a estrada. Durante a coleta, olhe
   esse ponto; não acompanhe a imagem na tela nem olhe para a lente.
2. Clique na janela da imagem para ela receber as teclas. Pressione **C**.
   Há 3 segundos de preparação e 3 segundos de coleta, com pelo menos 20 frames
   válidos. Mantenha olhos abertos, boca fechada e cabeça estável.
3. Ao aparecer `Postura OK`, escolha **S** para salvar somente a postura, ou **F**
   para avaliar os olhos. A etapa ocular é opcional.
4. Com **F**, mantenha a mesma postura e feche os dois olhos por cerca de 6 segundos
   (3 de preparação + 3 de coleta). Abra-os em seguida. Se a medição for aceita,
   pressione **S** para salvar e iniciar. Se houver sobreposição ou mudança da
   cabeça, use **F** para repetir ou **S** para seguir somente com a postura.
5. **C** refaz a calibração; **Q** sai. Nenhum evento de visão é registrado enquanto
   o assistente está coletando ou aguardando confirmação. A referência anterior
   no arquivo só é substituída ao salvar com S.

Se o rosto sumir ou o fluxo de frames parar, o trecho de coleta reinicia. O HUD
mostra a contagem; com FPS muito baixo a coleta pode levar mais de 3 segundos.
Se a etapa ocular ficar sem rosto, abra os olhos e pressione C para recomeçar.

Para reutilizar o perfil salvo:

```bash
bash tools/test_camera_gui.sh
```

Recalibre se mudar motorista, posição da câmera, banco, distância ou iluminação.
Resolução e modo IR/fonte são conferidos no arquivo; alterações físicas não são
detectadas automaticamente. Para testar sem IR, use o comando manual abaixo
sem `--infrared`, com outro nome de perfil e nova calibração.

```bash
QT_QPA_PLATFORM=xcb VGUARD_DATA=../dados-teste-gui .venv/bin/python -B vguard.py --camera-python /usr/bin/python3 --infrared --no-uart --no-gps --no-sync --calibration-file ../dados-teste-gui/calibration-driver.json --calibrate
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
