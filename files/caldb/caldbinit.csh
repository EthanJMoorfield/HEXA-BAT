# This is the Caldb initilization file for UNIX systems.  It sets up the
# 'CALDB' and 'CALDBCONFIG' environment variables.  The CALDB environment 
# variable should point at the top level directory of the Caldb and the 
# CALDBCONFIG environment variable should point at the file containing 
# paths to the index files.
# 
# This file is site dependent.  The directory paths may need to be
# edited when installing a Caldb.
#
#-----------------------------------------------------------------------

#setenv CALDB /FTP/caldb
#setenv CALDBCONFIG $CALDB/software/tools/caldb.config
#setenv CALDBALIAS $CALDB/software/tools/alias_config.fits

#
# MFC changed 20140623
#     if $CALDB* environment variables exist, then preserve them
#     
# NOTE TO USERS: YOU MAY NEED TO CHANGE THE DEFINITION
#      OF $CALDB, $CALDBCONFIG AND $CALDBALIAS TO MATCH 
#      YOUR LOCAL INSTALLATION
#
if ! $?CALDB then 
   setenv CALDB /FTP/caldb
   endif
if ! $?CALDBCONFIG then 
    setenv CALDBCONFIG $CALDB/software/tools/caldb.config
    endif
if ! $?CALDBALIAS then
    setenv CALDBALIAS $CALDB/software/tools/alias_config.fits
    endif

#echo "CALDBCONFIG set to $CALDBCONFIG"
#echo "CALDBALIAS set to $CALDBALIAS"
#echo "CALDB set to $CALDB"
   
