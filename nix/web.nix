{
  buildNpmPackage,
  importNpmLock,
  nodejs_22,
}:
buildNpmPackage {
  pname = "tokendrain-web";
  version = "0.1.0";
  src = ../web;
  nodejs = nodejs_22;
  npmDeps = importNpmLock { npmRoot = ../web; };
  npmConfigHook = importNpmLock.npmConfigHook;
  installPhase = ''
    runHook preInstall
    cp -r dist $out
    runHook postInstall
  '';
}
