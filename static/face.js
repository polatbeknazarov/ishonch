const FACE_BASE = "/static/vendor/face-api/";
const FACE_MODEL_URL = FACE_BASE + "model";

async function loadFaceModels() {
  if (window.__faceReady) return;
  const tf = faceapi.tf;
  if (tf.setWasmPaths) tf.setWasmPaths(FACE_BASE);
  let ready = false;
  for (const backend of ["webgl", "wasm", "cpu"]) {
    try {
      await tf.setBackend(backend);
      await tf.ready();
      ready = true;
      break;
    } catch (err) {
      console.warn("face backend", backend, err);
    }
  }
  if (!ready) throw new Error("Не удалось запустить распознавание лиц");
  await Promise.all([
    faceapi.nets.tinyFaceDetector.loadFromUri(FACE_MODEL_URL),
    faceapi.nets.faceLandmark68Net.loadFromUri(FACE_MODEL_URL),
    faceapi.nets.faceRecognitionNet.loadFromUri(FACE_MODEL_URL),
  ]);
  window.__faceReady = true;
}

async function openCamera(video) {
  const constraints = [
    { video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } }, audio: false },
    { video: { facingMode: "user" }, audio: false },
    { video: true, audio: false },
  ];
  let last = null;
  for (const opts of constraints) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia(opts);
      video.srcObject = stream;
      video.setAttribute("playsinline", "true");
      await video.play();
      return;
    } catch (err) {
      last = err;
    }
  }
  throw last || new Error("Камера недоступна");
}

async function faceDescriptor(video) {
  const det = await faceapi
    .detectSingleFace(video, new faceapi.TinyFaceDetectorOptions({ inputSize: 224, scoreThreshold: 0.45 }))
    .withFaceLandmarks()
    .withFaceDescriptor();
  if (!det) return null;
  return Array.from(det.descriptor);
}
