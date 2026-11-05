# Mudanças

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
