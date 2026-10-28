# Conector do Crop Steering

Liga este Home Assistant ao Cropilot. Com ele, o Cropilot lê os sensores de temperatura e umidade e comanda o
ar-condicionado, o desumidificador e as tomadas com função num espaço.

Ele só abre conexões de saída, com HTTPS, até o Cropilot, e não abre nenhuma porta no computador nem na sua rede.
A cada 10 minutos, ele pede ao Home Assistant uma leitura nova dos sensores que o Cropilot usa, para a leitura de um
valor que fica igual não ficar velha.
Use um Home Assistant só do cultivo: os aparelhos dele entram sozinhos no Cropilot, e uma tomada com função num
espaço pode ser ligada e desligada pelo Cropilot.
Para parear, gere o código em Dispositivos › Conexões, no Cropilot, e siga a aba Documentação depois de instalar.
