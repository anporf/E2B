import React, { useState, useRef } from 'react';
import { useSnackbar } from 'notistack';
import {
    displaySelector,
    revertAll,
    setShowCasesList,
    setShowUpload,
} from '@src/features/display/slice';
import { getCasesList } from '@src/features/cases-list/slice';
import { useDispatch, useSelector } from 'react-redux';
import {
    Button,
    Dialog,
    DialogActions,
    DialogContent,
    DialogTitle,
    Stack,
    Typography,
} from '@mui/material';
import { DataTable } from '../common/DataTable';
import { XML_API } from '@src/api/api.clients'; // Добавляем импорт XML_API

export const UploadXml = () => {
    const dispatch = useDispatch();
    const { showUpload } = useSelector(displaySelector);
    const { enqueueSnackbar } = useSnackbar();
    
    const [xmlFiles, setXmlFiles] = useState([]);
    const [showDataTable, setShowDataTable] = useState(false);
    const fileInputRef = useRef(null);

    const onHide = (event, reason) => {
        if (reason && reason === 'backdropClick') return;
        dispatch(setShowUpload(false));
    };

    const handleFileChange = (e) => {
        if (e.target.files && e.target.files.length > 0) {
            const files = Array.from(e.target.files);
            
            const filesWithValidation = files.map((file, index) => ({
                tempId: `temp_${index}_${Date.now()}`,
                fileName: file.name,
                file: file,
                isValid: false,
                missingFields: [
                    {
                        id: "c_1_2_date_creation",
                        label: "C.1.2 Date of Creation",
                        description: "Date when this report was first created",
                    },
                    {
                        id: "e_i_2_1b_reaction",
                        label: "E.i.2.1b Reaction/Event MedDRA term (PT)",
                        description: "MedDRA term for the reported reaction/event",
                    }
                ]
            }));
            
            setXmlFiles(filesWithValidation);
            setShowDataTable(true);
        }
    };

    const handleExcludeFile = (tempId) => {
        setXmlFiles(prevFiles => prevFiles.filter(file => file.tempId !== tempId));
        
        if (xmlFiles.length <= 1) {
            setShowDataTable(false);
        }
    };

    const handleImport = async (filesToImport) => {
        console.log("Importing files:", filesToImport);
        
        if (filesToImport.length === 0) {
            return;
        }
        
        try {
            const fileContentsPromises = filesToImport.map(fileObj => {
                return new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onload = (e) => {
                        resolve(e.target.result);
                    };
                    reader.onerror = (e) => {
                        reject(new Error(`Error reading file ${fileObj.fileName}`));
                    };
                    reader.readAsText(fileObj.file);
                });
            });
            
            const fileContents = await Promise.all(fileContentsPromises);
            
            const response = await XML_API.importMultipleXml(fileContents);
            
            console.log("Import multiple response:", response);
            
            const successCount = response.successful || 0;
            const failCount = response.failed || 0;
            
            let message = `Import complete:\n${successCount} files imported successfully\n${failCount} files failed\n\n`;
            
            if (response.results) {
                response.results.forEach((result, index) => {
                    const fileName = filesToImport[index]?.fileName || result.filename || `File ${index + 1}`;
                    message += `${fileName}: ${result.success ? "Imported (ID: " + result.id + ")" : "Failed - " + (result.error || "Unknown error")}\n`;
                });
            }
            
            if (successCount > 0) {
                enqueueSnackbar(`${successCount} files imported successfully`, { variant: 'success' });
                
                dispatch(getCasesList());
            }
            
            if (failCount > 0) {
                enqueueSnackbar(`${failCount} files failed to import`, { variant: 'error' });
            }
            
            console.log(message);
            
            if (successCount > 0) {
                dispatch(getCasesList());
            }
            
            dispatch(setShowUpload(false));
        } catch (error) {
            console.error("Error importing files:", error);
            alert(`Error importing files: ${error.message}`);
        }
        
        setShowDataTable(false);
        setXmlFiles([]);
    };

    const handleCloseDataTable = () => {
        setShowDataTable(false);
        setXmlFiles([]);
    };

    return (
        <>
            <Dialog open={showUpload} onClose={onHide}>
                <DialogTitle sx={{ fontSize: 30, color: 'black' }}>
                    {'Upload XML file(s)'}
                </DialogTitle>
                <DialogContent>
                    <Stack
                        direction="column"
                        spacing={2}
                        justifyContent="flex-start"
                    >
                        <Typography variant="body1" gutterBottom>
                            Select one or more XML files to import:
                        </Typography>
                        
                        <form method="post" encType="multipart/form-data">
                            <input
                                type="file"
                                name="file"
                                accept={".xml"}
                                onChange={handleFileChange}
                                multiple 
                                style={{ display: 'none' }}
                                ref={fileInputRef}
                            />
                            <Button
                                variant="contained"
                                color="primary"
                                onClick={() => fileInputRef.current.click()}
                            >
                                Choose Files
                            </Button>
                        </form>

                        {xmlFiles.length > 0 && !showDataTable && (
                            <Stack
                                direction="column"
                                spacing={2}
                                justifyContent="flex-start"
                            >
                                <Typography variant="body1">
                                    {`Selected ${xmlFiles.length} file(s)`}
                                </Typography>
                                <Button
                                    variant="contained"
                                    color="primary"
                                    onClick={() => setShowDataTable(true)}
                                >
                                    Review Files
                                </Button>
                            </Stack>
                        )}
                    </Stack>
                </DialogContent>
                <DialogActions>
                    <Button variant="outlined" onClick={onHide}>
                        Close
                    </Button>
                </DialogActions>
            </Dialog>
            
            {}
            <DataTable 
                open={showDataTable}
                onClose={handleCloseDataTable}
                itemsList={xmlFiles}
                onAction={handleImport}
                onExclude={handleExcludeFile}
                title="Import XML Files"
                actionButtonLabel="Import"
                canOpenItems={false}
                showIdColumn={false}
            />
        </>
    );
};