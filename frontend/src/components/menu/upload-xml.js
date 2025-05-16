import React, { useState, useRef } from 'react';
import { useSnackbar } from 'notistack';
import {
    displaySelector,
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
import { XML_API } from '@src/api/api.clients';

export const UploadXml = () => {
    const dispatch = useDispatch();
    const { showUpload } = useSelector(displaySelector);
    const { enqueueSnackbar } = useSnackbar();
    
    const [validationResults, setValidationResults] = useState([]);
    const [xmlFiles, setXmlFiles] = useState([]);
    const [showDataTable, setShowDataTable] = useState(false);
    const fileInputRef = useRef(null);

    const onHide = (event, reason) => {
        if (reason && reason === 'backdropClick') return;
        dispatch(setShowUpload(false));
    };

    const handleFileChange = async (e) => {
        if (e.target.files && e.target.files.length > 0) {
            const files = Array.from(e.target.files);
            console.log("Selected files:", files.map(f => f.name));
            
            try {
                const xmlContentsPromises = files.map((file) => {
                    return new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = (e) => resolve({
                            fileName: file.name,
                            content: e.target.result,
                            file: file
                        });
                        reader.onerror = (e) => reject(new Error(`Error reading file ${file.name}`));
                        reader.readAsText(file);
                    });
                });
    
                console.log("Reading files...");
                const filesData = await Promise.all(xmlContentsPromises);
                console.log("Files read successfully:", filesData.length);
                
                const xmlContents = filesData.map(fileData => fileData.content);
                
                console.log("Sending XML contents for validation...");
                const validationResults = await XML_API.validateImportXML(xmlContents);
                console.log("Validation results:", validationResults);
                
                if (!Array.isArray(validationResults)) {
                    console.error("validateMultipleXml did not return an array:", validationResults);
                    throw new Error("Invalid response format from validation API");
                }
                
                console.log("Processing validation results...");
                const processedResults = filesData.map((fileData, index) => {
                    console.log(`Processing file ${index}: ${fileData.fileName}`);
                    
                    const fileValidationResult = validationResults[index];
                    console.log(`Validation result for file ${index}:`, fileValidationResult);
                    
                    if (!fileValidationResult) {
                        console.warn(`No validation result for file ${index}`);
                        return {
                            tempId: `temp_${index}_${Date.now()}`,
                            fileName: fileData.fileName,
                            file: fileData.file,
                            isValid: false,
                            missingFields: [{
                                id: "validation_error",
                                label: "Validation Error",
                                description: "No validation result for this file",
                                externalKey: "system"
                            }]
                        };
                    }
                    
                    const allFieldErrors = [];
                    
                    Object.entries(fileValidationResult).forEach(([externalKey, fieldErrors]) => {
                        console.log(`Processing external key ${externalKey} for file ${index}`);
                        
                        Object.entries(fieldErrors).forEach(([fieldId, errorMessage]) => {
                            allFieldErrors.push({
                                id: fieldId,
                                label: fieldId,
                                description: String(errorMessage),
                                externalKey: externalKey
                            });
                        });
                    });
                    
                    console.log(`Field errors for file ${index}:`, allFieldErrors);
                    
                    return {
                        tempId: `temp_${index}_${Date.now()}`,
                        fileName: fileData.fileName,
                        file: fileData.file,
                        isValid: allFieldErrors.length === 0,
                        missingFields: allFieldErrors
                    };
                });
                
                console.log("Processed results:", processedResults);
                setValidationResults(processedResults);
                
                setXmlFiles(processedResults);
                setShowDataTable(true);
            } catch (error) {
                console.error("Error processing files:", error);
                enqueueSnackbar(`Error processing files: ${error.message}`, { variant: "error" });
            }
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