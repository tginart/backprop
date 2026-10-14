/** Decode class-label images without a canvas readback.
 * Architecture: authenticated blob -> browser image codec -> raw RGBA bytes ->
 * caller colors the classes. Main caller: imageViewer. Canvas privacy features
 * can perturb pixel reads and invent class IDs, so masks use WebCodecs directly.
 * Spec: https://www.w3.org/TR/webcodecs/#imagedecoder-interface
 */
export async function decodeMaskPixels(url) {
  if (!url.startsWith('blob:')) throw new Error('The mask API returned an unexpected URL.');
  if (typeof ImageDecoder === 'undefined') throw new Error('Segmentation masks require a browser with ImageDecoder support.');
  const response = await fetch(url);
  if (!response.ok) throw new Error('Mask bytes could not be loaded.');
  const blob = await response.blob(), type = blob.type || 'image/png';
  if (!await ImageDecoder.isTypeSupported(type)) throw new Error(`This browser cannot decode ${type} segmentation masks.`);
  const decoder = new ImageDecoder({data: await blob.arrayBuffer(), type, colorSpaceConversion: 'none'});
  let frame;
  try {
    ({image: frame} = await decoder.decode({frameIndex: 0}));
    const width = frame.displayWidth, height = frame.displayHeight;
    const data = new Uint8ClampedArray(width * height * 4);
    await frame.copyTo(data, {format: 'RGBA', layout: [{offset: 0, stride: width * 4}]});
    return {width, height, data};
  } finally {
    frame?.close();
    decoder.close();
  }
}
