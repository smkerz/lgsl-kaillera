<?php
error_reporting(E_ALL);
ini_set('display_errors', 1);

echo "<h3>Kaillera / EmuLinker - Diagnostic complet</h3>";
echo "<b>Time:</b> " . date('H:i:s') . "<br><br>";

// =============================================
// 1) Master Server (kaillera.com)
// =============================================
echo "<h4>1) Master Server (kaillera.com)</h4>";
$ch = curl_init('http://www.kaillera.com/raw_server_list2.php');
curl_setopt($ch, CURLOPT_RETURNTRANSFER, 1);
curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 3);
curl_setopt($ch, CURLOPT_TIMEOUT, 5);
$data = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

echo "HTTP {$code}, " . strlen($data) . " bytes<br>";
if ($data && $code == 200) {
  $lines = explode("\n", trim($data));
  $found = false;
  for ($i = 0; $i + 1 < count($lines); $i += 2) {
    if (strpos($lines[$i + 1], '141.95.154.67') !== false) {
      $parts = explode(";", $lines[$i + 1]);
      echo "Server: <b>" . htmlspecialchars(trim($lines[$i])) . "</b><br>";
      echo "Raw data: <code>" . htmlspecialchars($lines[$i + 1]) . "</code><br>";
      echo "Players: <b style='color:" . ($parts[1] !== '0/100' ? 'green' : 'red') . "'>" . htmlspecialchars($parts[1]) . "</b><br>";
      $found = true;
    }
  }
  if (!$found) echo "<span style='color:red'>Server 141.95.154.67 NOT found in master list!</span><br>";
} else {
  echo "<span style='color:red'>Master server unreachable</span><br>";
}

// =============================================
// 2) Direct UDP Query - HELLO handshake
// =============================================
echo "<br><h4>2) Direct UDP Query (HELLO handshake)</h4>";

$targets = ['141.95.154.67', '127.0.0.1'];
foreach ($targets as $target) {
  echo "<b>{$target}:27888</b> &rarr; ";
  $fp = @fsockopen("udp://{$target}", 27888, $errno, $errstr, 3);
  if (!$fp) {
    echo "Connection failed: {$errstr}<br>";
    continue;
  }
  stream_set_timeout($fp, 3);
  fwrite($fp, "HELLO0.83\x00");
  $response = fread($fp, 4096);
  fclose($fp);

  if ($response) {
    echo strlen($response) . " bytes<br>";

    // Hex dump
    echo "<code>Hex: ";
    for ($j = 0; $j < min(strlen($response), 64); $j++) {
      echo sprintf("%02X ", ord($response[$j]));
    }
    if (strlen($response) > 64) echo "...";
    echo "</code><br>";

    // ASCII
    echo "<code>ASCII: " . htmlspecialchars(preg_replace('/[^\x20-\x7E]/', '.', $response)) . "</code><br>";

    if (strpos($response, "HELLOD00D") !== false) {
      echo "<span style='color:green'>HELLOD00D OK - Server is alive</span><br>";

      // Parse data after HELLOD00D\0
      $helloPos = strpos($response, "HELLOD00D");
      $after = substr($response, $helloPos + 10); // skip HELLOD00D + \0
      if (strlen($after) > 0) {
        echo "Extra data after HELLOD00D: " . strlen($after) . " bytes: <code>";
        for ($j = 0; $j < strlen($after); $j++) {
          echo sprintf("%02X ", ord($after[$j]));
        }
        echo "</code><br>";

        // Try to interpret as port (2 bytes little-endian)
        if (strlen($after) >= 2) {
          $assignedPort = ord($after[0]) | (ord($after[1]) << 8);
          echo "Possible assigned port: <b>{$assignedPort}</b><br>";
        }
      }
    } else {
      echo "<span style='color:red'>No HELLOD00D in response</span><br>";
    }
  } else {
    echo "<span style='color:orange'>No response (timeout)</span><br>";
  }
}

// =============================================
// 3) EmuLinker HTTP Interface
// =============================================
echo "<br><h4>3) EmuLinker HTTP Interface</h4>";
echo "EmuLinker-SF peut avoir une interface web. Test de plusieurs URLs...<br><br>";

$http_targets = [
  'http://141.95.154.67:27888/',
  'http://127.0.0.1:27888/',
  'http://141.95.154.67:8080/',
  'http://127.0.0.1:8080/',
  'http://141.95.154.67:27888/access',
  'http://127.0.0.1:27888/access',
];

foreach ($http_targets as $url) {
  echo "<b>{$url}</b> &rarr; ";
  $ch = curl_init($url);
  curl_setopt($ch, CURLOPT_RETURNTRANSFER, 1);
  curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 2);
  curl_setopt($ch, CURLOPT_TIMEOUT, 3);
  curl_setopt($ch, CURLOPT_FOLLOWLOCATION, 1);
  $result = curl_exec($ch);
  $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
  $curl_error = curl_error($ch);
  curl_close($ch);

  if ($result && $http_code >= 200 && $http_code < 400) {
    echo "<span style='color:green'>HTTP {$http_code}</span>, " . strlen($result) . " bytes<br>";
    echo "<pre style='background:#222;color:#0f0;padding:8px;max-height:200px;overflow:auto'>" . htmlspecialchars(substr($result, 0, 1000)) . "</pre>";
  } else {
    echo "<span style='color:red'>Failed</span> (HTTP {$http_code}" . ($curl_error ? ", {$curl_error}" : "") . ")<br>";
  }
}

// =============================================
// 4) Check EmuLinker Docker - file access
// =============================================
echo "<br><h4>4) EmuLinker Config Files</h4>";
echo "Recherche de fichiers de config EmuLinker accessibles...<br>";

$config_paths = [
  '/home/docker/emulinker/emulinker.cfg',
  '/home/docker/emulinker/conf/emulinker.cfg',
  '/home/docker/emulinker/emulinker.properties',
  '/home/docker/emulinker/conf/emulinker.properties',
  '/home/docker/emulinker/config.properties',
  '/home/docker/emulinker/docker-compose.yml',
  '/home/docker/emulinker/docker-compose.yaml',
];

foreach ($config_paths as $path) {
  if (file_exists($path)) {
    echo "<b style='color:green'>FOUND: {$path}</b><br>";
    $content = @file_get_contents($path);
    if ($content) {
      echo "<pre style='background:#222;color:#0f0;padding:8px;max-height:300px;overflow:auto'>" . htmlspecialchars($content) . "</pre>";
    } else {
      echo "(readable but empty or permission denied)<br>";
    }
  } else {
    echo "<span style='color:gray'>{$path} - not found</span><br>";
  }
}

// Try listing the emulinker directory
$emudir = '/home/docker/emulinker/';
if (is_dir($emudir)) {
  echo "<br><b>Contents of {$emudir}:</b><br>";
  $files = @scandir($emudir);
  if ($files) {
    foreach ($files as $f) {
      if ($f === '.' || $f === '..') continue;
      $full = $emudir . $f;
      $type = is_dir($full) ? '[DIR]' : filesize($full) . ' bytes';
      echo "  {$f} ({$type})<br>";
    }
  } else {
    echo "  Permission denied<br>";
  }
} else {
  echo "<br><span style='color:orange'>{$emudir} not accessible from PHP</span><br>";
}

// =============================================
// 5) Summary
// =============================================
echo "<br><h4>5) Résumé</h4>";
echo "<p>Si le master server montre 0 joueurs meme quand tu es connecte, c'est EmuLinker qui ne rapporte pas les joueurs.</p>";
echo "<p><b>Solutions possibles :</b></p>";
echo "<ul>";
echo "<li>Si une interface HTTP EmuLinker repond (section 3), on peut l'utiliser pour avoir le vrai nombre de joueurs</li>";
echo "<li>Si on peut lire la config EmuLinker (section 4), on peut verifier les settings masterList</li>";
echo "<li>Verifier dans la config EmuLinker : <code>masterList.touchKaillera=true</code> et <code>masterList.touchInterval</code></li>";
echo "</ul>";
