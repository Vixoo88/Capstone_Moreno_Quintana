// Opcional: rotación automática lenta del carrusel
document.addEventListener('DOMContentLoaded', () => {
  const el = document.querySelector('#heroCarousel');
  if (el) {
    const carousel = new bootstrap.Carousel(el, {
      interval: 6000,
      ride: 'carousel',
      pause: false,
      touch: true,
      wrap: true
    });
  }
});
