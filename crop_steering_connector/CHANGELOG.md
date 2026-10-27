# Mudanças

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
