orig="2026-10-26-13-07-15_INBOX_04eaff14b30e_bbvaAsuntoIncidenciaApartadoAhorro.txt";
rec1="2026-10-07-12-24-41_INBOX_694ab54f2e9b_errorApartados.txt";
rec2="2026-10-04-14-16-11_INBOX_d1b081c36787_nuevoErrorAppAndroidBbva.txt";
rec3="2026-09-25-13-41-53_INBOX_9c8db2db1cfe_gestiónDeSolicitudesBbva.txt";
rec4="2026-09-18-13-23-56_INBOX_8e9e58077d6a_reclamaciónErrorEnConfiguraciónDeTransferenciaPeriódicaAppAn.txt";
rec5="2026-09-13-10-07-07_INBOX_859dd0152d2b_externalUtf8QEgaranteCertificaciã³nDeEnvãODeCorreoElectrã³ni.txt";
rec6="2026-09-10-21-51-11_INBOX_282628a3397f_reReclamaciónErrorEnConfiguraciónDeTransferenciaPeriódicaApp.txt";
rec7="2026-09-10-15-46-00_INBOX_df46f2641ebe_reExternalBbvaAppAndroidAhorrosFrecuentes.txt";
rec8="2026-09-10-12-58-22_INBOX_40be1db66414_bbvaAppAndroidAhorrosFrecuentes.txt"
i=1;j=8;
while (( i <= j ));do
  v="id=\$(echo \"\${rec${i}}\" | cut -c '27-38' );echo \"Buscando \$id\";cp *\${id}*.* 2026-10-26reclamacionApp";
  eval "${v};";
  i=$((i+1));
done;
