# Mudanças

## 0.2.17

- Enquanto o umidificador trabalha, o Cropilot pode desligar o desumidificador, com a proteção de 10 minutos, e o religa
  quando o umidificador para. O Conector vigia cada desumidificador que o Cropilot desligou assim e o religa sozinho,
  nunca antes de 10 minutos do desligamento, quando a plataforma fica 20 minutos sem responder, quando ela passa 25
  minutos sem confirmar esse desligamento, quando o pareamento acaba e quando ele é pareado de novo. Se você ligar o
  desumidificador nesse tempo, ou tirá-lo da função dele no Cropilot, o Conector deixa de vigiá-lo.
- Reserva do clima: o Cropilot pode mandar ao Conector, junto com cada resposta, as trocas de período do
  ar-condicionado e do desumidificador de um dia à frente. Se a plataforma ficar 20 minutos sem responder, o Conector
  aplica sozinho as trocas que ela não fez, nos horários em que a iluminação acende e apaga, com os valores que o
  Cropilot mandou: só no ar-condicionado que está refrigerando e no desumidificador que está ligado, sem nunca ligar
  nem desligar um aparelho. Isso só passa a valer quando o Cropilot mandar esses horários. Sem internet na casa ou com
  o Home Assistant parado, a reserva do clima não faz nada.
- Antes de voltar para a 0.2.16, espere o Conector falar com o Cropilot de novo: a versão anterior não religa sozinha
  o desumidificador que o Cropilot desligou para o umidificador trabalhar, e, com a plataforma fora do ar, ele fica
  desligado até ela voltar; e as trocas que a reserva do clima fez e ainda não mandou se perdem na volta.

## 0.2.16

- Rega de reserva: junto com cada resposta, o Cropilot manda ao Conector os disparos da rega automática de um dia à
  frente, com a dose de cada um. Se a plataforma ficar fora do ar e não mandar um disparo, o Conector espera 5 minutos
  depois do horário e dá esse disparo sozinho, desligando a bomba na dose, com o pulso gravado na tomada ainda de
  reserva. Ele nunca faz sozinho o flush nem o disparo de teste e evita repetir um disparo: num caso raro, quando a
  confirmação de um disparo se perde no caminho, o Cropilot avisa para você conferir a água dos vasos. Quando o contato
  volta, os disparos que ele deu aparecem no histórico do Cropilot.
- Antes de cada pulso, o Conector confere no Home Assistant o pulso gravado na tomada: se ele não for o que o Cropilot
  espera, o Conector não liga a bomba. Sem internet na casa, a tomada Smart Life não recebe o comando: nada liga a
  bomba, e só o pulso gravado nela a desliga.
- Isso só passa a valer quando o Cropilot mandar esses horários. Antes de voltar para a 0.2.15, espere o Conector falar
  com o Cropilot de novo: os disparos que ele deu sozinho e ainda não mandou se perdem na volta.

## 0.2.15

- Quando o Cropilot marca a hora de um comando, o Conector espera essa hora antes de mandá-lo ao aparelho, sem segurar
  os outros comandos. Assim, num espaço com mais de um ar-condicionado ou desumidificador, os compressores não partem
  todos juntos: entre duas partidas fica o intervalo que o Cropilot pediu, também quando o Conector volta a se conectar
  e recebe vários comandos de uma vez. Um espaço com mais de um ar-condicionado ou desumidificador precisa desta versão
  ou de uma mais nova.

## 0.2.14

- Uma lâmpada inteligente pode ser a iluminação do espaço, como a tomada: o Conector mostra ao Cropilot se ela está
  acesa ou apagada, e o Cropilot pode acendê-la e apagá-la nos horários da fase. O Cropilot nunca muda o brilho, a cor
  nem os efeitos da lâmpada, e o Conector recusa qualquer outro pedido para ela.
- Antes de voltar para a 0.2.13, tire a lâmpada da função Iluminação: a versão anterior não reconhece lâmpadas e
  descarta os horários de iluminação que o Conector guardou, os das tomadas também, até o Cropilot mandá-los de novo.

## 0.2.13

- O Conector pode mandar ao Cropilot o fabricante, o modelo, o código do modelo e a versão do firmware de cada aparelho,
  como o Home Assistant os mostra, quando o Cropilot pedir. Assim, a página do aparelho mostra o modelo, e você vê quais
  funções a equipe já testou com ele. O número de série e os endereços de rede do aparelho nunca saem do Home Assistant.

## 0.2.12

- Logo depois de você mudar o pulso da bomba, o primeiro disparo, o de teste incluído, já segue o pulso novo. Antes, o
  Conector ainda podia desligar a bomba no fim do pulso antigo.
- A bomba de rega pode ser uma válvula do Home Assistant: o Cropilot abre a válvula nos disparos, e o Conector a fecha se
  ela ficar aberta além do pulso ou abrir fora de um disparo. A válvula que demora a fechar tem até 30 segundos antes de
  o Conector contar uma tentativa como falha. Antes de voltar para a 0.2.11, tire a válvula da função Rega: a versão
  anterior não reconhece válvulas e descarta tudo o que a vigia da bomba guardou.
- O Conector já sabe desligar a bomba na dose exata de cada horário, com o pulso da tomada só como reserva. Isso vem
  desligado e só passa a valer quando o Cropilot avisar; até lá, a rega segue como antes.
- O Conector manda ao Cropilot o estado ao ligar da tomada, quando o Home Assistant o mostra. Assim, o Cropilot confere
  se ele está em “Desligado”, para a bomba não ligar sozinha quando a energia volta.
- O Conector manda ao Cropilot os avisos de problema que o Home Assistant mostra no próprio desumidificador ou
  umidificador, como o tanque cheio, só para leitura. Assim, o Cropilot avisa quando o tanque do desumidificador enche.

## 0.2.11

- O Conector passa a mandar ao Cropilot o modo em que o desumidificador está e os modos que ele oferece, só para
  leitura. O Cropilot não muda o modo do aparelho.

## 0.2.10

- O Cropilot pode acender e apagar a iluminação do espaço nos horários da fase, sem o timer da tomada. Você liga isso
  no espaço, no Cropilot, e desliga o timer da tomada no app Smart Life. O Conector guarda os horários e segue com eles
  por até 7 dias se perder o contato com o Cropilot. A tomada Smart Life só recebe o comando pela internet: sem
  internet ou com o Home Assistant desligado, a iluminação fica como está até eles voltarem.
- Se o Home Assistant for reinstalado do zero, sem backup, o Cropilot passa a reconhecer os aparelhos compatíveis com
  Smart Life que você já usava, mesmo quando o Home Assistant dá a eles um identificador novo. Assim, a função de cada
  aparelho no espaço pode passar sozinha para ele.
- Para isso, o Conector manda ao Cropilot, para cada aparelho, o nome das integrações do Home Assistant em que ele está
  e um código embaralhado de cada uma, feito a partir do código que a integração dá ao aparelho. O Cropilot não guarda
  esses códigos: usa só o dos aparelhos compatíveis com Smart Life, embaralhado de novo, e descarta os outros na hora.
- Isso só funciona para os aparelhos que o Conector já leu nesta versão antes da reinstalação: vale atualizar antes de
  qualquer reinstalação.

## 0.2.9

- O umidificador que demora a ligar continua sob os cuidados do Conector: ele não solta mais o aparelho só porque o Home
  Assistant ainda o mostra desligado logo depois do comando do Cropilot.
- O Conector só avisa que desligou o umidificador depois que o Home Assistant aceita o pedido. Se o Home Assistant não
  aceitar, ele tenta de novo e, na terceira vez, avisa o Cropilot da falha.

## 0.2.8

- O umidificador que o Cropilot manda ligar no mesmo instante em que a conexão é revogada desliga na hora, como os
  outros, em vez de ficar ligado até os 20 minutos sem resposta da plataforma.

## 0.2.7

- O Cropilot pode ligar e desligar o umidificador do espaço, só na germinação, na clonagem e no vegetativo. Para
  usar o umidificador no Cropilot, o conector precisa estar nesta versão ou numa mais nova.
- O Conector desliga o umidificador que o Cropilot ligou quando a plataforma para de responder por 20 minutos ou
  quando a conexão é revogada. Ele também desliga esse umidificador quando é pareado de novo, e avisa o Cropilot de
  cada desligamento.
- O conector manda ao Cropilot o que o umidificador está fazendo, como umidificar ou parado, quando o Home Assistant
  tem essa informação.

## 0.2.6

- O conector manda ao Cropilot o nível da bateria dos aparelhos que já entram, quando o Home Assistant tem essa
  informação, como no sensor de temperatura e umidade à pilha. Em Dispositivos, o Cropilot mostra esse nível e avisa
  quando a bateria de um sensor do espaço fica fraca. O sensor de bateria em si não vira um aparelho no Cropilot.

## 0.2.5

- Um aparelho novo no Home Assistant, como um sensor de temperatura e umidade, chega ao Cropilot como um aparelho só e
  com o nome dele desde o primeiro envio do conector, em vez de aparecer em Dispositivos com uma linha para cada leitura.
- Depois de “Sincronizar”, em Dispositivos, o conector confirma ao Cropilot que leu de novo os aparelhos no Home
  Assistant. Se essa leitura falhar, o Cropilot pede outra vez sozinho.

## 0.2.4

- Em Dispositivos, no Cropilot, “Sincronizar” faz o conector ler de novo no Home Assistant os aparelhos, os nomes
  deles e as leituras, e mandar tudo ao Cropilot logo em seguida. Um aparelho novo ou um nome trocado chega sem
  esperar os 10 minutos de sempre. O conector traz só o que o Home Assistant já tem.
- No ar-condicionado, o tempo sem mudança agora conta só pela temperatura e pela umidade que ele mede. Mudar a
  temperatura programada ou o modo não faz mais uma leitura parada parecer nova.

## 0.2.3

- A cada 10 minutos, o conector pede ao Home Assistant uma leitura nova dos sensores de temperatura e umidade e dos
  aparelhos de clima que o Cropilot usa. Assim, a leitura de um sensor que fica um tempo com o mesmo valor continua
  valendo no Cropilot. O pedido só atualiza a leitura e não muda nenhum aparelho.
- O conector também diz ao Cropilot há quanto tempo cada um desses valores não muda. Assim, o Cropilot mostra quando
  um valor fica igual por muito tempo, sem deixar de usar a leitura.

## 0.2.2

- O código de pareamento agora é gerado em Dispositivos › Conexões, no Cropilot, e é ali que você desconecta o Home
  Assistant. A documentação e as mensagens da aba Registro apontam para esse lugar. O conector funciona como antes.
- No Cropilot, os aparelhos deste Home Assistant agora entram sozinhos: em Dispositivos, “Tirar” tira um aparelho, e
  “Liberar” o traz de volta. Uma tomada só recebe comando quando tem função num espaço. A documentação explica essa
  regra, e as mensagens da aba Registro agora falam dos aparelhos que o Cropilot usa.

## 0.2.1

- O conector nunca grava o pulso da bomba de rega: quem grava é você, na tomada, pelo app Smart Life, e o Cropilot só
  confere o pulso gravado.
- Para aquecer, o conector manda ao ar-condicionado o modo aquecer e a temperatura juntos, numa chamada só.
- Textos revistos na documentação do conector.

## 0.2.0

- O Cropilot pode colocar o ar-condicionado no modo aquecer quando a planta fica fria demais ou para ajudar o
  desumidificador.
- O Cropilot pode mudar a umidade programada no desumidificador, para evitar desligá-lo.
- Bomba de rega: o conector liga a tomada da bomba nos disparos do Cropilot, confere se ela desliga sozinha no fim do
  pulso e tenta desligá-la se ficar ligada além dele ou ligar fora de um disparo.
- Ao iniciar, o conector tenta desligar a bomba que encontrar ligada fora de um disparo.
- A lista das bombas fica guardada neste Home Assistant, e a conferência continua sem contato com o Cropilot.
- O conector manda o nome de cada aparelho, como aparece no Smart Life ou no Home Assistant, e a potência e a energia
  das tomadas que medem o consumo.
- Você gera o código de pareamento em Perfil › Conexões, no Cropilot, e libera ali os aparelhos que ele comanda.

## 0.1.0

- Primeira versão: o Cropilot lê os sensores de temperatura e umidade e comanda o ar-condicionado, o desumidificador
  e as tomadas que a equipe do Cropilot liberar.
